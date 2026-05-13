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

詳細設計: docs/intent/stackchan_vessel.md (SAIVerse 本体側) v0.5 §C-1
"""
import logging
import sys
import threading
from pathlib import Path
from typing import Any, Iterator

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


def _pcm_iterator_from_queue(queue) -> Iterator[bytes]:
    """voice-tts の subscribe_pcm が返す Queue から PCM chunks を yield する。

    Queue は ``Optional[bytes]`` を要素に持ち、``None`` (sentinel) で終端する。
    ``requests`` の ``data=`` に直接渡せる sync iterator として返す
    (chunked transfer encoding は requests が自動的に行う)。
    """
    from queue import Empty

    while True:
        try:
            chunk = queue.get(timeout=120.0)
        except Empty:
            LOGGER.warning(
                "stackchan speak_hook: PCM queue idle timeout, abort"
            )
            return
        if chunk is None:
            # voice-tts 側で close_pcm_stream が呼ばれた = 発話終了
            return
        if chunk:
            yield chunk


def _post_pcm_in_background(
    message_id: str,
    pcm_token: str,
    pcm_url: str,
    sample_rate: int,
    vessel_id: str,
) -> None:
    """別スレッドで HTTP POST を実行 (= server_hook を block しない)。

    voice-tts の subscribe_pcm は subscribe-before-open 対応なので、まだ
    voice-tts 側で open_pcm_stream が呼ばれてなくても Queue が確保される。
    開始タイミングは voice-tts と speak_hook の並行起動で前後する可能性が
    あるが、subscribe_pcm の Queue で吸収される。
    """
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

    # requests は標準ライブラリじゃないが、SAIVerse 本体で広く使われている
    # ので addon でも利用可能と仮定する。Phase 1' 着手前に確認済み。
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

    try:
        # chunked transfer encoding は requests が iterator を data= に
        # 渡されると自動的に有効化する。Transfer-Encoding: chunked が
        # 自動付与され、Content-Length は省略される。
        resp = requests.post(
            pcm_url,
            data=_pcm_iterator_from_queue(queue),
            headers=headers,
            timeout=(5, 300),  # connect 5s, read 300s (発話最大 5 分想定)
        )
        if resp.status_code == 200:
            LOGGER.info(
                "stackchan speak_hook: PCM POST done vessel_id=%s msg_id=%s "
                "result=%s",
                vessel_id, message_id, resp.text[:200],
            )
        else:
            LOGGER.warning(
                "stackchan speak_hook: PCM POST failed vessel_id=%s msg_id=%s "
                "status=%d body=%s",
                vessel_id, message_id, resp.status_code, resp.text[:500],
            )
    except requests.RequestException as exc:
        LOGGER.warning(
            "stackchan speak_hook: PCM POST request error vessel_id=%s "
            "msg_id=%s: %s",
            vessel_id, message_id, exc,
        )
    except Exception:
        LOGGER.exception(
            "stackchan speak_hook: unexpected error vessel_id=%s msg_id=%s",
            vessel_id, message_id,
        )


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

    LOGGER.info(
        "stackchan speak_hook: forwarding audio to vessel_id=%s persona=%s msg=%s",
        vessel.vessel_id, persona_id, message_id,
    )

    # HTTP POST は subscribe_pcm Queue の drain が完了するまでブロックするので
    # 別スレッドで走らせる (= server_hook の dispatch を妨げない)。
    thread = threading.Thread(
        target=_post_pcm_in_background,
        args=(
            message_id,
            pcm_token,
            pcm_url,
            _VOICE_TTS_SAMPLE_RATE,
            vessel.vessel_id,
        ),
        daemon=True,
        name=f"stackchan-pcm-{message_id[:8]}",
    )
    thread.start()
