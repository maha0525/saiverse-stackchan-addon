"""Stack-chan Vessel: persona_speak server_hook ハンドラ。

ペルソナが発話したとき、そのペルソナが Vessel Building に居る (= 物理身体に
降りている) 場合のみ、voice-tts が生成中の音声 stream を Stack-chan device
に転送する (audio_stream_bridge 経由)。

voice-tts も同じ persona_speak hook を持つので両方が並行起動する:
  - voice-tts: 自前で TTS 合成 → audio_stream に push (= 音源生成)
  - 本 hook: 同じ audio_stream に subscribe (= 音源利用)
合成は voice-tts 側で 1 回しか走らない (二重合成にならない)。

詳細設計: docs/intent/stackchan_vessel.md の E. 音声出力 (TTS 経路)
"""
import logging
import sys
from pathlib import Path
from typing import Any

# addon_loader の spec_from_file_location 経由ロードでは __package__ が
# 設定されないため相対 import が動かない。同梱モジュールを絶対 import
# するためにパック自身のディレクトリを sys.path に追加する。
_PACK_DIR = str(Path(__file__).parent)
if _PACK_DIR not in sys.path:
    sys.path.insert(0, _PACK_DIR)

from vessel_manager import get_vessel_manager  # noqa: E402
from audio_stream_bridge import start_streaming  # noqa: E402

LOGGER = logging.getLogger(__name__)


def on_persona_speak(
    persona_id: str,
    building_id: str,
    message_id: str,
    **_kwargs: Any,
) -> None:
    """ペルソナ発話イベント。Vessel device が紐付けされていれば音声転送を起動。

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
    # 同じ building_id に接続中の vessel を探す
    # (Vessel Building は capacity=1 なので 1 ペルソナ + 1 vessel しか居ない)
    target_session = None
    for session in vm.list_sessions():
        if session.building_id == building_id:
            target_session = session
            break

    if target_session is None:
        LOGGER.debug(
            "stackchan speak_hook: no vessel connected in building_id=%s, skip",
            building_id,
        )
        return

    if target_session.event_loop is None:
        LOGGER.warning(
            "stackchan speak_hook: vessel session has no event_loop "
            "vessel_id=%s (pairing flow regression?)",
            target_session.vessel_id,
        )
        return

    LOGGER.info(
        "stackchan speak_hook: forwarding audio to vessel_id=%s persona=%s msg=%s",
        target_session.vessel_id, persona_id, message_id,
    )
    start_streaming(message_id, target_session.ws, target_session.event_loop)
