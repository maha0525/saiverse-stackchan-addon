"""voice-tts の audio_stream pub/sub から MP3 chunks を取得し、Stack-chan device に
WebSocket binary frame で転送するブリッジ。

設計 (docs/intent/stackchan_vessel.md の E. 音声出力 案 D):
  - voice-tts と stackchan_addon が両方 persona_speak server_hook を購読する
  - voice-tts 側: 自前で TTS 合成 → ``audio_stream.push_pcm`` で MP3 化 + 配信
  - 本ブリッジ: 同じ ``audio_stream.subscribe`` で chunks を pop し、device に
    WebSocket binary で転送する
  - 合成は voice-tts 側で 1 回しか走らない (二重合成にならない)

スレッド設計:
  server_hooks は ThreadPoolExecutor (max_workers=4) 上で呼ばれるため、本ブリッジも
  バックグラウンドスレッドで動かす。FastAPI WebSocket は asyncio event loop バウンド
  なので、別スレッドからの送信は ``asyncio.run_coroutine_threadsafe`` で event loop
  にスケジュールする。
"""
import asyncio
import logging
import threading
from queue import Empty
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket

LOGGER = logging.getLogger(__name__)

# voice-tts の audio_stream モジュール (遅延 import で初回時にロード)
_audio_subscribe: Optional[Callable[[str], Any]] = None


def _ensure_voice_tts_loaded() -> bool:
    """voice-tts の audio_stream.subscribe を import (1 度だけ)。

    voice-tts は addon_loader 経由で ``tools._loaded.speak.audio_stream`` として
    sys.modules に登録される。stackchan_addon の起動時点で voice-tts がまだ
    ロードされていない可能性があるので、persona_speak 発火時に lazy import する。
    """
    global _audio_subscribe
    if _audio_subscribe is not None:
        return True
    try:
        from tools._loaded.speak.audio_stream import subscribe  # type: ignore[import-not-found]
        _audio_subscribe = subscribe
        LOGGER.info("audio_stream_bridge: voice-tts audio_stream loaded")
        return True
    except ImportError as e:
        LOGGER.warning(
            "audio_stream_bridge: voice-tts audio_stream import failed: %s "
            "(voice-tts addon が無効化されているか、ロード順序の問題)", e,
        )
        return False


def start_streaming(
    message_id: str,
    ws: "WebSocket",
    loop: asyncio.AbstractEventLoop,
) -> None:
    """voice-tts の audio_stream に subscribe して MP3 chunks を device に送る。

    バックグラウンドスレッドで動作。1 message_id = 1 ストリーム。
    """
    if not _ensure_voice_tts_loaded():
        return

    thread = threading.Thread(
        target=_streaming_worker,
        args=(message_id, ws, loop),
        daemon=True,
        name=f"vessel-audio-{message_id[:8]}",
    )
    thread.start()


def _streaming_worker(
    message_id: str,
    ws: "WebSocket",
    loop: asyncio.AbstractEventLoop,
) -> None:
    """別スレッドで audio_stream から MP3 chunks を pop して WebSocket に送る。"""
    if _audio_subscribe is None:
        return
    try:
        queue = _audio_subscribe(message_id)
        LOGGER.info("audio_stream_bridge: subscribed msg_id=%s", message_id)

        # S->D: 開始通知 (device がバッファ初期化等を準備するきっかけ)
        _send_json_threadsafe(
            ws, loop, {"type": "audio_start", "message_id": message_id}
        )

        chunk_count = 0
        total_bytes = 0
        while True:
            try:
                chunk = queue.get(timeout=30.0)
            except Empty:
                LOGGER.warning(
                    "audio_stream_bridge: timeout waiting for chunk msg_id=%s",
                    message_id,
                )
                break

            if chunk is None:
                # 終端 sentinel (voice-tts 側で audio_stream.close() 後に送られる)
                LOGGER.info(
                    "audio_stream_bridge: end of stream msg_id=%s chunks=%d bytes=%d",
                    message_id, chunk_count, total_bytes,
                )
                break

            _send_bytes_threadsafe(ws, loop, chunk)
            chunk_count += 1
            total_bytes += len(chunk)

        # S->D: 終端通知 (device がバッファを flush して再生終了するきっかけ)
        _send_json_threadsafe(
            ws, loop, {"type": "audio_end", "message_id": message_id}
        )
    except Exception:
        LOGGER.exception("audio_stream_bridge: worker error msg_id=%s", message_id)


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
) -> None:
    """別スレッドから WebSocket.send_bytes() を実行する。"""
    try:
        future = asyncio.run_coroutine_threadsafe(ws.send_bytes(data), loop)
        future.result(timeout=5.0)
    except Exception as e:
        LOGGER.warning(
            "audio_stream_bridge: send_bytes failed (%d bytes): %s",
            len(data), e,
        )
