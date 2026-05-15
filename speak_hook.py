"""Stack-chan Vessel: persona_speak server_hook ハンドラ (v0.5: HTTP POST 経路)。

ペルソナが発話したとき、そのペルソナが Vessel Building に居る (= 物理身体に
降りている) 場合のみ、voice-tts が生成中の音声 stream を stackchan-mcp gateway
の HTTP PCM endpoint (POST /pcm) に chunked transfer encoding で転送する。

v0.4 までは addon が自前で WebSocket gateway を持って device 側に PCM を直送
していたが、v0.5 では stackchan-mcp gateway を本体 MCP client が subprocess
として管理する設計に変わったため、addon は subprocess 越しに HTTP POST で PCM
を渡す形になった。

voice-tts も同じ persona_speak hook を持つので両方が並行起動する:
  - voice-tts: 自前で TTS 合成 → audio_stream に push (= 音源生成)
  - 本 hook: 同じ audio_stream に subscribe (= 音源利用) → HTTP POST で gateway
合成は voice-tts 側で 1 回しか走らない (二重合成にならない)。

連続再生 (Phase 1: FIFO wait):
    新しい persona_speak が来た時、 同じ vessel に向けて流れている既存 POST
    があれば **その完了を待ってから** 新 POST を開始する (= preempt しない)。
    これで 「同 pulse 内で連発した発話」 が全て途切れず順次再生される。
    待ち合わせは ``_ActivePostState.completed`` を使い、 register は dict
    末尾置換で chain する (= 3 連発も 1 → 2 → 3 の順に正しく流れる)。

    旧 「常時 preempt」 動作は ``abort_requested`` event の経路を残しつつ
    無効化してある (Phase 1 では誰も .set() しない)。 Phase 2 で pulse_id
    比較を入れて 「同 pulse → wait / 別 pulse → preempt」 で発動させる。

詳細設計: docs/intent/voice_tts_playback_queue.md (= Phase 1)、
docs/intent/stackchan_vessel.md (SAIVerse 本体側) v0.5 §C-1
"""
import logging
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

# addon_loader の spec_from_file_location 経由ロードでは __package__ が
# 設定されないため相対 import が動かない。同梱モジュールを絶対 import
# するためにパック自身のディレクトリを sys.path に追加する。
_PACK_DIR = str(Path(__file__).parent)
if _PACK_DIR not in sys.path:
    sys.path.insert(0, _PACK_DIR)

from vessel_manager import get_vessel_manager  # noqa: E402

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"

# voice-tts の PCM サンプルレート。GPT-SoVITS の出力に合わせて 32 kHz。
# stackchan-mcp gateway 側で 16 kHz にリサンプルされる。
_VOICE_TTS_SAMPLE_RATE = 32000


# ----- vessel 単位 in-flight POST tracking (Phase 1: FIFO wait chain) -----


@dataclass
class _ActivePostState:
    """1 vessel に対して進行中の POST の状態。

    Phase 1 (現状) では FIFO wait で順次再生される (= ``abort_requested``
    は誰も .set() しない)。 Phase 2 で pulse_id を比較し、 別 pulse の
    persona_speak が来た時のみ ``abort_requested`` を立てて旧 POST を
    preempt する設計に拡張する予定。

    Field の意味:
      - ``message_id``: voice-tts の audio_stream key、 ログ識別用
      - ``pulse_id``: 発話を生んだ pulse 識別子 (Phase 2 の preempt 判定軸)
      - ``abort_requested``: 立てると iterator が yield を止めて
        requests.post の body を終端させ、 gateway が ``tts_lock`` を解放
      - ``completed``: thread が finally まで抜けた合図 (= POST が成功 /
        失敗 / 早期 return のいずれでも立つ)、 後続 POST の wait 解除に使う
    """
    message_id: str
    pulse_id: Optional[str] = None
    abort_requested: threading.Event = field(default_factory=threading.Event)
    completed: threading.Event = field(default_factory=threading.Event)


_active_posts: Dict[str, _ActivePostState] = {}
_active_posts_lock = threading.Lock()


def _register_at_tail(
    vessel_id: str, new_state: _ActivePostState
) -> Optional[_ActivePostState]:
    """新 state を vessel の queue 末尾として register、 直前 state を返す。

    Phase 1 の FIFO chaining: 連発 1 → 2 → 3 はそれぞれ:
      - 1 が register: prev=None
      - 2 が register: prev=1 → 2 は 1.completed を待ってから POST 開始
      - 3 が register: prev=2 → 3 は 2.completed を待ってから POST 開始

    結果として 2 は 1 の後ろ、 3 は 2 の後ろに並び、 順次再生される。
    各 state の completed.set() が次の wait を解除する chain で、 GIL 下の
    register-then-wait の race も無い (register をアトミックにやれば、
    後続から見た 「直前 state」 が確実に 1 つ前になる)。

    旧 state が既に completed している (= POST が高速に終わった) 場合は
    None を返し、 呼び出し側は wait をスキップして即 POST 開始する。
    """
    with _active_posts_lock:
        prev = _active_posts.get(vessel_id)
        if prev is not None and prev.completed.is_set():
            # 既に終わってる残骸。 待つ必要なし。
            prev = None
        _active_posts[vessel_id] = new_state
        if prev is not None:
            LOGGER.debug(
                "stackchan speak_hook: queued behind in-flight POST "
                "vessel_id=%s prev_msg=%s new_msg=%s",
                vessel_id, prev.message_id, new_state.message_id,
            )
    return prev


def _clear_active_post(vessel_id: str, state: _ActivePostState) -> None:
    """POST 完了時に state を切り離す。 既に別の新しい state に置き換わって
    いれば dict には触らず、 ``state.completed`` だけ立てる (= 後続 wait
    が解放される)。"""
    with _active_posts_lock:
        if _active_posts.get(vessel_id) is state:
            _active_posts.pop(vessel_id, None)
    state.completed.set()


def _load_voice_tts_subscribe():
    """voice-tts の audio_stream PCM 経路を遅延 import する。

    voice-tts addon が無効 or 古い版で PCM 経路を持たない環境では、本 hook
    自体は no-op として動作させる (= persona_speak の他 hook を妨げない)。
    """
    try:
        from tools._loaded.speak.audio_stream import (  # type: ignore[import-not-found]
            subscribe_pcm,
            get_pcm_stream_info,
        )
        return subscribe_pcm, get_pcm_stream_info
    except ImportError as exc:
        LOGGER.warning(
            "stackchan speak_hook: voice-tts audio_stream PCM unavailable: %s "
            "(voice-tts addon が無効化されているか、PCM 経路未対応の旧版)",
            exc,
        )
        return None, None


def _load_addon_params() -> dict:
    """addon の AddonConfig (global params) を取得する。"""
    try:
        from saiverse.addon_config import get_params
        return get_params(ADDON_NAME, persona_id=None) or {}
    except Exception as exc:
        LOGGER.warning(
            "stackchan speak_hook: failed to load addon params: %s", exc
        )
        return {}


def _wait_first_chunk(queue, timeout_s: float = 60.0) -> bytes | None:
    """voice-tts subscribe_pcm の最初の chunk を取得する。

    GPT-SoVITS の最初の chunk 生成には GPU で 10〜20 秒程度かかる
    (= 観測例: 1221 文字で 15.94 秒)。speak_hook が subscribe_pcm 直後
    に HTTP POST を開始すると、その 10〜20 秒は chunked transfer の
    chunk が来ない idle 時間になり、device 側で「もう音が来ない」と
    判定されて speaking → listening に勝手に戻ってしまう (= 観測例:
    speaking 遷移から 18 秒で listening、結果として発話の冒頭だけ届い
    て後は沈黙、というユーザー観察)。

    そこで POST を始める前に最初の chunk が来るのを待つ。最初の chunk
    が来てから POST を開始すれば、以降は voice-tts が PCM を実時間で
    連続生成するので chunked transfer の idle 時間は短く済む (= フレー
    ム間隔 = ms オーダー)。

    ``None`` を返す場合: voice-tts が音を 1 つも流さず close した、
    または queue idle が ``timeout_s`` を超えた。どちらも発話を諦める。
    """
    from queue import Empty

    try:
        first = queue.get(timeout=timeout_s)
    except Empty:
        LOGGER.warning(
            "stackchan speak_hook: first chunk timeout (%.1fs), abort", timeout_s
        )
        return None
    if first is None:
        LOGGER.debug(
            "stackchan speak_hook: voice-tts closed before first chunk, abort"
        )
        return None
    if not first:
        # 空 chunk は通常起きないが、来たら次の chunk を待たずに諦める。
        # voice-tts 側のバグの可能性があるので WARNING で残す。
        LOGGER.warning(
            "stackchan speak_hook: empty first chunk received, abort"
        )
        return None
    return first


def _pcm_iterator_after_first(
    first_chunk: bytes,
    queue,
    message_id: str = "?",
    abort_event: Optional[threading.Event] = None,
) -> Iterator[bytes]:
    """``_wait_first_chunk`` で先取りした最初の chunk + 続きの queue
    を結合した sync iterator を返す。

    POST 開始時点で確実に最初の chunk が yield されるので chunked
    transfer の最初の data 送信までの idle が短く済む。以降は voice-tts
    の連続生成 cadence (= 30 kHz mono 16-bit @ realtime = 60 KB/s) に
    乗って chunk が流れる。

    観測用ログ (Phase 1' 残課題「発話末尾切れ」検証用): voice-tts の
    投入 pace が realtime かどうか、iterator が None sentinel まで
    確実に最後まで yield しているか、POST body 終了 (= return) から
    server response 返却までの遅延を測るための時刻情報を残す。
    """
    from queue import Empty
    import time

    start_t = time.monotonic()
    total_bytes = len(first_chunk)
    chunk_count = 1
    if abort_event is not None and abort_event.is_set():
        # 起動直後に既に abort されているケース (= 連続発話が高速に来た)。
        # first_chunk すら yield せずに終端する。
        LOGGER.info(
            "stackchan speak_hook[%s]: aborted before first yield",
            message_id,
        )
        return
    LOGGER.debug(
        "stackchan speak_hook[%s]: iter yield #%d t=+%.2fs bytes=%d cum=%d",
        message_id, chunk_count, 0.0, len(first_chunk), total_bytes,
    )
    yield first_chunk
    # abort signal を頻繁にチェックするため queue.get は 1 秒ずつポーリング
    # する。 連続 idle が _MAX_IDLE_SECONDS を超えたら voice-tts 側が死んだ
    # と判断して諦める (= 旧来の 120s 1 発 wait と同じ実効動作)。
    _MAX_IDLE_SECONDS = 120.0
    idle_seconds = 0.0
    while True:
        if abort_event is not None and abort_event.is_set():
            # 後発の persona_speak が来て preempt された。 ここで return すると
            # requests.post の body が終端、 gateway が tts_lock を解放する。
            LOGGER.info(
                "stackchan speak_hook[%s]: aborted at t=+%.2fs chunks=%d "
                "cum=%d (preempted by newer persona_speak)",
                message_id, time.monotonic() - start_t, chunk_count, total_bytes,
            )
            return
        try:
            chunk = queue.get(timeout=1.0)
            idle_seconds = 0.0
        except Empty:
            idle_seconds += 1.0
            if idle_seconds >= _MAX_IDLE_SECONDS:
                LOGGER.warning(
                    "stackchan speak_hook[%s]: PCM queue idle timeout at "
                    "t=+%.2fs cum=%d, abort",
                    message_id, time.monotonic() - start_t, total_bytes,
                )
                return
            continue
        if chunk is None:
            # voice-tts 側で close_pcm_stream が呼ばれた = 発話終了
            LOGGER.debug(
                "stackchan speak_hook[%s]: None sentinel received at t=+%.2fs "
                "chunks=%d cum=%d (= POST body end)",
                message_id, time.monotonic() - start_t, chunk_count, total_bytes,
            )
            return
        if chunk:
            chunk_count += 1
            total_bytes += len(chunk)
            # 連続 chunk のログは uniform 間隔で間引く (= 多すぎ防止)。
            # voice-tts の投入 pace を測れる粒度として 0.5 秒毎に出す。
            elapsed = time.monotonic() - start_t
            if chunk_count <= 3 or (chunk_count % 20 == 0):
                LOGGER.debug(
                    "stackchan speak_hook[%s]: iter yield #%d t=+%.2fs bytes=%d cum=%d",
                    message_id, chunk_count, elapsed, len(chunk), total_bytes,
                )
            yield chunk


def _post_pcm_in_background(
    message_id: str,
    pcm_token: str,
    pcm_url: str,
    sample_rate: int,
    vessel_id: str,
    state: _ActivePostState,
    prev_state: Optional[_ActivePostState],
) -> None:
    """別スレッドで HTTP POST を実行 (= server_hook を block しない)。

    Phase 1 (FIFO wait): ``prev_state`` が non-None なら、 まずその
    completed を待つ (= 直前 POST が完全に終わるまで自分は POST しない)。
    timeout は 1 発話の最大想定再生時間 (= 5 分) + マージン。 タイムアウト
    したら警告だけ残して進む (= 何かが詰まっていても次の発話を永遠に
    block しないための安全弁)。

    voice-tts の subscribe_pcm は subscribe-before-open 対応なので、まだ
    voice-tts 側で open_pcm_stream が呼ばれてなくても Queue が確保される。
    開始タイミングは voice-tts と speak_hook の並行起動で前後する可能性が
    あるが、subscribe_pcm の Queue で吸収される。

    重要: 最初の chunk が来るまで HTTP POST 自体を開始しない。voice-tts
    の最初の chunk 生成は GPT-SoVITS で 10〜20 秒かかるので、POST を先
    に開けてしまうと chunked transfer の冒頭で「データが来ない」期間が
    長くなり、device 側が「もう音が来ない」と判定して speaking から
    listening に戻ってしまう (詳細は ``_wait_first_chunk`` の docstring
    参照)。

    ``state.abort_requested`` が立った場合 (= Phase 2 の別 pulse preempt)、
    first chunk 待ち中なら諦めて return、 既に POST 中なら iterator が
    止まって body 終端する。 Phase 1 では誰も .set() しないので発火しない。

    関数末尾の finally で必ず ``_clear_active_post`` を呼ぶので
    ``state.completed`` は確実に立つ (= 次の persona_speak が待ち合わせ
    から抜けられる)。
    """
    import time as _time

    try:
        # Phase 1: FIFO wait — 直前 POST が完了するまで block する。
        if prev_state is not None:
            _PREV_WAIT_TIMEOUT = 600.0  # 10 分: 5 分発話 + 余裕
            wait_start = _time.monotonic()
            if not prev_state.completed.wait(timeout=_PREV_WAIT_TIMEOUT):
                LOGGER.warning(
                    "stackchan speak_hook[%s]: prev POST did not complete "
                    "within %.0fs vessel_id=%s prev_msg=%s — proceeding "
                    "anyway (gateway lock may still be held)",
                    message_id, _PREV_WAIT_TIMEOUT, vessel_id,
                    prev_state.message_id,
                )
            else:
                LOGGER.debug(
                    "stackchan speak_hook[%s]: prev POST completed after "
                    "%.2fs wait, starting our turn",
                    message_id, _time.monotonic() - wait_start,
                )

        subscribe_pcm, _ = _load_voice_tts_subscribe()
        if subscribe_pcm is None:
            return

        queue = subscribe_pcm(message_id)
        if queue is None:
            LOGGER.warning(
                "stackchan speak_hook: subscribe_pcm returned None msg_id=%s",
                message_id,
            )
            return

        # abort_requested が立っていれば first chunk すら待たず諦める。
        # Phase 1 では発火しない (Phase 2 で別 pulse preempt 時に発火)。
        if state.abort_requested.is_set():
            LOGGER.info(
                "stackchan speak_hook[%s]: aborted before first chunk wait",
                message_id,
            )
            return

        # voice-tts が最初の chunk を生成し終えるまで block して待つ。GPU 計算
        # 時間で 10〜20 秒程度かかる。この間 HTTP POST は開かない。
        first_chunk = _wait_first_chunk(queue)
        if first_chunk is None:
            return
        if state.abort_requested.is_set():
            # 最初の chunk 取得中に preempt された (= まれ)。
            LOGGER.info(
                "stackchan speak_hook[%s]: aborted after first chunk wait",
                message_id,
            )
            return

        # requests は標準ライブラリじゃないが、SAIVerse 本体で広く使われて
        # いるので addon でも利用可能と仮定する。Phase 1' 着手前に確認済み。
        try:
            import requests
        except ImportError:
            LOGGER.error(
                "stackchan speak_hook: 'requests' library not installed, "
                "cannot POST PCM to gateway"
            )
            return

        headers = {
            "Content-Type": "application/octet-stream",
            "X-Sample-Rate": str(sample_rate),
            "X-Channels": "1",
            "X-Message-Id": message_id,
        }
        if pcm_token:
            headers["Authorization"] = f"Bearer {pcm_token}"

        post_start = _time.monotonic()
        LOGGER.debug(
            "stackchan speak_hook[%s]: POST start (= first chunk arrived, "
            "starting requests.post)",
            message_id,
        )
        try:
            # chunked transfer encoding は requests が iterator を data= に
            # 渡されると自動的に有効化する。Transfer-Encoding: chunked が
            # 自動付与され、Content-Length は省略される。
            # 最初の chunk は _wait_first_chunk で取得済みなので、ここに渡す
            # iterator は最初の chunk + 残りを yield する形にする。
            resp = requests.post(
                pcm_url,
                data=_pcm_iterator_after_first(
                    first_chunk, queue, message_id,
                    abort_event=state.abort_requested,
                ),
                headers=headers,
                timeout=(5, 300),  # connect 5s, read 300s (発話最大 5 分想定)
            )
            LOGGER.debug(
                "stackchan speak_hook[%s]: POST returned status=%d after %.2fs",
                message_id, resp.status_code,
                _time.monotonic() - post_start,
            )
            if resp.status_code == 200:
                LOGGER.info(
                    "stackchan speak_hook: PCM POST done vessel_id=%s "
                    "msg_id=%s result=%s",
                    vessel_id, message_id, resp.text[:200],
                )
            else:
                LOGGER.warning(
                    "stackchan speak_hook: PCM POST failed vessel_id=%s "
                    "msg_id=%s status=%d body=%s",
                    vessel_id, message_id, resp.status_code,
                    resp.text[:500],
                )
        except requests.RequestException as exc:
            LOGGER.warning(
                "stackchan speak_hook[%s]: PCM POST request error after "
                "%.2fs vessel_id=%s: %s",
                message_id, _time.monotonic() - post_start, vessel_id, exc,
            )
        except Exception:
            LOGGER.exception(
                "stackchan speak_hook: unexpected error vessel_id=%s "
                "msg_id=%s",
                vessel_id, message_id,
            )
    finally:
        # POST が成功 / 失敗 / 早期 return のいずれでも、 必ず
        # state.completed を立てて次の persona_speak の待ち合わせを
        # 解放する。 これがないと preempt の待ち side が永遠に block する。
        _clear_active_post(vessel_id, state)


def on_persona_speak(
    persona_id: str,
    building_id: str,
    message_id: str,
    **_kwargs: Any,
) -> None:
    """ペルソナ発話イベント。Vessel device が紐付けされていれば PCM 転送を起動。

    Args:
        persona_id: 発話したペルソナの ID
        building_id: 発話が行われた Building の ID
        message_id: 発話メッセージの ID (voice-tts の audio_stream key)
        **_kwargs: text_raw / text_for_voice / pulse_id / source / metadata 等。
            本ハンドラでは使用しないが、本体側 payload が増えても壊れないよう受ける。
    """
    if not persona_id or not message_id or not building_id:
        return

    vm = get_vessel_manager()
    vessel = vm.get_vessel_for_persona(persona_id, building_id)
    if vessel is None:
        LOGGER.debug(
            "stackchan speak_hook: no vessel bound to persona=%s building=%s, skip",
            persona_id, building_id,
        )
        return

    params = _load_addon_params()
    pcm_token = str(params.get("pcm_token") or "")
    capture_port = str(params.get("gateway_capture_port") or "8766")
    # gateway は SAIVerse プロセスと同じホストの subprocess なので 127.0.0.1
    # で到達できる。VISION_HOST (LAN IP) は device → gateway 用で、本 hook
    # の SAIVerse → gateway は loopback で十分。
    pcm_url = f"http://127.0.0.1:{capture_port}/pcm"

    pulse_id = _kwargs.get("pulse_id")
    LOGGER.info(
        "stackchan speak_hook: forwarding audio to vessel_id=%s persona=%s "
        "msg=%s pulse=%s",
        vessel.vessel_id, persona_id, message_id, pulse_id,
    )

    # Phase 1 (FIFO wait): 自分を queue 末尾として register し、 直前 state
    # を取得。 直前 wait + POST 本体は別スレッドで実行する (= server_hook
    # の dispatch を妨げない)。 register をアトミックに行うことで、 同時
    # 発火した 2 連発でも 「2 個目から見た直前 = 1 個目」 が確実に決まる
    # (= chain が壊れない)。
    state = _ActivePostState(message_id=message_id, pulse_id=pulse_id)
    prev_state = _register_at_tail(vessel.vessel_id, state)

    thread = threading.Thread(
        target=_post_pcm_in_background,
        args=(
            message_id,
            pcm_token,
            pcm_url,
            _VOICE_TTS_SAMPLE_RATE,
            vessel.vessel_id,
            state,
            prev_state,
        ),
        daemon=True,
        name=f"stackchan-pcm-{message_id[:8]}",
    )
    thread.start()
