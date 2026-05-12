"""voice-tts の audio_stream PCM 経路から PCM bytes を取得し、Stack-chan device に
WebSocket binary frame で転送するブリッジ。

設計 (PCM 直送):
  - voice-tts 側: synthesize_stream → audio_stream.push_pcm_chunk (生の PCM)
  - 本ブリッジ: audio_stream.subscribe_pcm で PCM chunks を pop し、device に
    WebSocket binary で転送する
  - device 側 (ESP32) は MP3 decode 不要、playRaw に直接渡す
  - 旧設計 (MP3 経路) は libhelix の frame sync 失敗で「途中で切れて次が始まる」
    挙動の原因になっていたため廃止

スレッド設計:
  server_hooks は ThreadPoolExecutor (max_workers=4) 上で呼ばれるため、本ブリッジも
  バックグラウンドスレッドで動かす。FastAPI WebSocket は asyncio event loop バウンド
  なので、別スレッドからの送信は ``asyncio.run_coroutine_threadsafe`` で event loop
  にスケジュールする。
"""
import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from queue import Empty
from typing import Any, Callable, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket

LOGGER = logging.getLogger(__name__)


# 同一 vessel に対する同時 streaming は 1 つだけ許可する。新しい発話 (=
# 新しい start_streaming 呼び出し) が来たら、既存の worker に abort signal を
# 立ててすぐ終わらせる (= 割り込み再生)。device 側でも audio_start 受信時に
# 再生中の音を停止 + ring buffer をクリアする。
@dataclass
class _WorkerState:
    message_id: str
    abort: bool = False


_active_workers: Dict[str, _WorkerState] = {}  # vessel_id -> state
_workers_lock = threading.Lock()

# WS binary frame 1 個あたりの最大送信サイズ。
# Stack-chan が使う ``WebSocketsClient`` (links2004/WebSockets) のデフォルト
# ``WEBSOCKETS_MAX_DATA_SIZE`` は 15 KB (ESP32 系)。これを超える payload を
# 受信すると ESP32 側 library が silent disconnect する。安全マージン込みで
# 8 KB に分割。device 側は ring buffer に順次 push するだけなので、PCM byte
# stream として分割しても影響なし (16-bit alignment は維持する: _alignment を 2 で取る)。
_MAX_WS_FRAME_BYTES = 8 * 1024

# PCM 16-bit のため 2 byte 境界で分割 (奇数で切ると sample が割れる)
_PCM_SAMPLE_BYTES = 2  # int16

# 「常に N 秒分の音声を先送りしている」状態を維持するよう、送信ペースを
# 再生速度に合わせる。device 側 ring buffer (Stack-chan: 32 KB ≒ 500ms 分の
# PCM @ 32 kHz mono) より小さい lead_time を設定して、初回の burst でも
# overflow しないようにする。200ms = 12.8 KB の先送り。
_LEAD_TIME_SECONDS = 0.2

# voice-tts の audio_stream モジュール (遅延 import で初回時にロード)
_pcm_subscribe: Optional[Callable[[str], Any]] = None
_pcm_get_info: Optional[Callable[[str], Any]] = None


def _ensure_voice_tts_loaded() -> bool:
    """voice-tts の audio_stream PCM 経路を import (1 度だけ)。"""
    global _pcm_subscribe, _pcm_get_info
    if _pcm_subscribe is not None:
        return True
    try:
        from tools._loaded.speak.audio_stream import (  # type: ignore[import-not-found]
            subscribe_pcm,
            get_pcm_stream_info,
        )
        _pcm_subscribe = subscribe_pcm
        _pcm_get_info = get_pcm_stream_info
        LOGGER.info("audio_stream_bridge: voice-tts audio_stream (PCM) loaded")
        return True
    except ImportError as e:
        LOGGER.warning(
            "audio_stream_bridge: voice-tts audio_stream PCM import failed: %s "
            "(voice-tts addon が無効化されているか、PCM 経路未対応の旧版)", e,
        )
        return False


def start_streaming(
    message_id: str,
    ws: "WebSocket",
    loop: asyncio.AbstractEventLoop,
    vessel_id: str,
) -> None:
    """voice-tts の audio_stream に subscribe して PCM chunks を device に送る。

    バックグラウンドスレッドで動作。同一 vessel の既存 worker は abort される
    (= 割り込み再生)。device 側も audio_start で前再生を停止する。
    """
    if not _ensure_voice_tts_loaded():
        return

    # 既存 worker があれば abort 立てて、新 worker 用の state を登録する
    state = _WorkerState(message_id=message_id)
    with _workers_lock:
        old = _active_workers.get(vessel_id)
        if old is not None:
            old.abort = True
            LOGGER.info(
                "audio_stream_bridge: aborting previous worker vessel_id=%s "
                "old_msg=%s new_msg=%s",
                vessel_id, old.message_id, message_id,
            )
        _active_workers[vessel_id] = state

    thread = threading.Thread(
        target=_streaming_worker,
        args=(message_id, ws, loop, vessel_id, state),
        daemon=True,
        name=f"vessel-audio-{message_id[:8]}",
    )
    thread.start()


def _streaming_worker(
    message_id: str,
    ws: "WebSocket",
    loop: asyncio.AbstractEventLoop,
    vessel_id: str,
    state: _WorkerState,
) -> None:
    """別スレッドで audio_stream の PCM queue から chunks を pop して WebSocket に送る。"""
    if _pcm_subscribe is None or _pcm_get_info is None:
        return
    try:
        # subscribe-before-open: voice-tts が open_pcm_stream を呼ぶ前に
        # subscribe しても placeholder ctx が作られて queue が返る。
        queue = _pcm_subscribe(message_id)
        if queue is None:
            LOGGER.warning(
                "audio_stream_bridge: subscribe_pcm returned None msg_id=%s",
                message_id,
            )
            return

        LOGGER.info("audio_stream_bridge: subscribed_pcm msg_id=%s", message_id)

        audio_start_sent = False
        sample_rate = 0
        channels = 0
        bytes_per_sec = 0.0
        chunk_count = 0
        total_bytes = 0
        ws_alive = True
        sent_audio_seconds = 0.0
        send_start = None
        # 累積無音時間 (timeout=1s で抜けた回数 × 1s)。120s 経過したら諦める。
        idle_seconds = 0.0
        _MAX_IDLE_SECONDS = 120.0

        while True:
            # 割り込み: 他の発話が来て自分が abort されたら即終了
            if state.abort:
                LOGGER.info(
                    "audio_stream_bridge: worker aborted msg_id=%s "
                    "chunks=%d bytes=%d (replaced by newer streaming)",
                    message_id, chunk_count, total_bytes,
                )
                break

            try:
                # 短く区切って abort signal を頻繁にチェック
                chunk = queue.get(timeout=1.0)
                idle_seconds = 0.0
            except Empty:
                idle_seconds += 1.0
                if idle_seconds >= _MAX_IDLE_SECONDS:
                    LOGGER.warning(
                        "audio_stream_bridge: idle timeout msg_id=%s (%ds elapsed)",
                        message_id, int(idle_seconds),
                    )
                    break
                continue

            if chunk is None:
                # 終端 sentinel
                LOGGER.info(
                    "audio_stream_bridge: end of stream msg_id=%s chunks=%d bytes=%d",
                    message_id, chunk_count, total_bytes,
                )
                break

            # 最初の chunk 到達時点で open_pcm_stream は完了しているはず。
            # ここで sample_rate / channels を取得して audio_start を送る。
            if not audio_start_sent:
                info = _pcm_get_info(message_id)
                if info is None:
                    LOGGER.error(
                        "audio_stream_bridge: pcm stream info unavailable "
                        "msg_id=%s (open_pcm_stream missing?)",
                        message_id,
                    )
                    break
                sample_rate, channels = info
                bytes_per_sec = float(sample_rate * channels * _PCM_SAMPLE_BYTES)
                _send_json_threadsafe(ws, loop, {
                    "type": "audio_start",
                    "message_id": message_id,
                    "sample_rate": sample_rate,
                    "channels": channels,
                    "format": "pcm_s16le",
                })
                audio_start_sent = True
                LOGGER.info(
                    "audio_stream_bridge: audio_start sent msg_id=%s sr=%d ch=%d",
                    message_id, sample_rate, channels,
                )

            # 8 KB 分割送信 + sub chunk 単位の pacing。
            # voice-tts の 1 chunk は GPT-SoVITS の generator 出力単位 (= 数秒分の
            # PCM = 100 KB 級) になることがある。これを sub chunk に分けずに
            # 一括 pacing すると、sub chunks がバーストで送られて device 側
            # ring buffer (32 KB) を即時 overflow させる。
            # → sub chunk (8 KB ≒ 125ms) ごとに pacing する。
            send_failed = False
            for offset in range(0, len(chunk), _MAX_WS_FRAME_BYTES):
                sub = chunk[offset:offset + _MAX_WS_FRAME_BYTES]
                # 16-bit PCM の境界保証 (chunk 末尾が奇数 byte でも安全)
                if len(sub) % _PCM_SAMPLE_BYTES != 0:
                    sub = sub[:-(len(sub) % _PCM_SAMPLE_BYTES)]
                    if not sub:
                        continue
                if not _send_bytes_threadsafe(ws, loop, sub):
                    LOGGER.info(
                        "audio_stream_bridge: ws send failed, abort msg_id=%s "
                        "chunks=%d bytes=%d sub_offset=%d/%d",
                        message_id, chunk_count, total_bytes,
                        offset, len(chunk),
                    )
                    send_failed = True
                    break
                total_bytes += len(sub)

                # sub chunk 単位の pacing
                # = sub chunk = 8 KB / 64 KB/s = 0.128 秒分の PCM
                if send_start is None:
                    send_start = time.monotonic()
                sent_audio_seconds += len(sub) / bytes_per_sec
                elapsed = time.monotonic() - send_start
                ahead = sent_audio_seconds - elapsed
                if ahead > _LEAD_TIME_SECONDS:
                    time.sleep(ahead - _LEAD_TIME_SECONDS)
            if send_failed:
                ws_alive = False
                break
            chunk_count += 1

        # S->D: 終端通知。abort された場合は送らない (新 audio_start が device に
        # 既に届いてるので、ここで audio_end を送ると新発話の audioPlaying が
        # false にされて再生が止まる)。
        if ws_alive and audio_start_sent and not state.abort:
            _send_json_threadsafe(
                ws, loop, {"type": "audio_end", "message_id": message_id}
            )
    except Exception:
        LOGGER.exception("audio_stream_bridge: worker error msg_id=%s", message_id)
    finally:
        # tracking dict から自分を削除 (= 自分が現在登録されている state の場合のみ。
        # 自分が既に新 state に上書きされていれば触らない)
        with _workers_lock:
            current = _active_workers.get(vessel_id)
            if current is state:
                _active_workers.pop(vessel_id, None)


def _send_json_threadsafe(
    ws: "WebSocket",
    loop: asyncio.AbstractEventLoop,
    msg: dict,
) -> None:
    """別スレッドから WebSocket.send_json() を実行する。"""
    try:
        future = asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)
        future.result(timeout=5.0)
    except Exception as e:
        LOGGER.warning(
            "audio_stream_bridge: send_json failed type=%s: %s",
            msg.get("type"), e,
        )


def _send_bytes_threadsafe(
    ws: "WebSocket",
    loop: asyncio.AbstractEventLoop,
    data: bytes,
) -> bool:
    """別スレッドから WebSocket.send_bytes() を実行する。

    Returns:
        True: 送信成功
        False: 送信失敗 (device disconnect / ASGI close 済み / timeout 等)
    """
    try:
        future = asyncio.run_coroutine_threadsafe(ws.send_bytes(data), loop)
        future.result(timeout=5.0)
        return True
    except Exception as e:
        LOGGER.warning(
            "audio_stream_bridge: send_bytes failed (%d bytes): %s",
            len(data), e,
        )
        return False
