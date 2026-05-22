"""Stack-chan device からの音声入力経路 (v0.7: Gemini inline 認識)。

stackchan-mcp gateway は device 主導 listen (LCD タッチ / ウェイクワード /
ボタン) の Opus 音声を Ogg コンテナにパックして HTTP POST してくる。本
モジュールはその POST を受信し、 ~/.saiverse/audio/ に Ogg ファイルとして
保存して、 Vessel Building の occupant ペルソナへ
``manager.handle_user_input_stream`` 経由でユーザー発言として注入する。

ペルソナ側はその発言が物理マイク経由であることを ``metadata.source``
("stackchan_voice") で識別でき、 Gemini ペルソナは ``metadata.media[]`` の
audio エントリを ``inline_data`` として直接理解して返答する。 v1.0 で実装
済みのユーザー添付音声経路 (`/upload-audio` → `metadata.media[]` → Gemini
``inline_data``) の出口に合流する。

詳細設計: docs/intent/stackchan_vessel.md (SAIVerse 本体側) v0.7 §C-2 / §G

経路全体:
[Stack-chan device] LCD タッチ短押し or ウェイクワード → 録音開始
  ↓ Opus フレーム送信
[stackchan-mcp gateway] listen.stop → Ogg コンテナ化
  ↓ POST /api/addon/saiverse-stackchan-addon/audio-in
[本 endpoint] verify_token → ファイル保存 → handle_user_input_stream
  ↓ metadata.media[]
[Gemini ペルソナ] inline_data で音を直接理解して返答

認証:
    stackchan-mcp gateway は env ``STACKCHAN_AUDIO_HOOK_TOKEN`` を Bearer
    で送る。 未設定なら gateway 側で ``STACKCHAN_TOKEN`` (= master_token)
    にフォールバックするので、 本 endpoint も master_token と同じ
    ``verify_token`` 経路で検証する。
"""
import asyncio
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, status

# 同梱モジュールを絶対 import するためにパック自身のディレクトリを
# sys.path に追加する (= avatar_loader / speak_hook と同じパターン)。
_PACK_DIR = str(Path(__file__).parent)
if _PACK_DIR not in sys.path:
    sys.path.insert(0, _PACK_DIR)

from saiverse.addon_deps import get_manager  # noqa: E402
from vessel_manager import get_vessel_manager  # noqa: E402

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"

# 本モジュールが export する router。 api_routes.py で
# ``router.include_router(audio_router)`` する形で本体に mount される。
audio_router = APIRouter()


def _save_ogg_capture(body: bytes) -> tuple[Path, str]:
    """受信した Ogg バイト列を ``~/.saiverse/audio/`` に保存する。

    Returns:
        ``(dest_path, dest_name)``。 dest_name は ``saiverse://audio/<name>``
        URI の suffix として metadata.media[] にも載る。

    Raises:
        OSError: ファイル書き込み失敗時。 呼び出し側で 500 に変換する。
    """
    dest_dir = Path.home() / ".saiverse" / "audio"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_name = (
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_"
        f"{uuid.uuid4().hex}_stackchan.ogg"
    )
    dest_path = dest_dir / dest_name
    dest_path.write_bytes(body)
    return dest_path, dest_name


def _drain_stream_sync(stream) -> None:
    """``handle_user_input_stream`` の generator を消費する。

    SAIVerseManager.handle_user_input_stream は同期 generator
    (= chunk を yield する)。 本 endpoint は HTTP レスポンスを即返した
    あとに background 実行で drain したいので、 sync な消費関数を別
    thread に投げる形にする。 ペルソナの応答そのものは Building の他
    経路 (= 通常のチャット履歴 streaming) でユーザーに届く。
    """
    try:
        for _chunk in stream:
            pass
    except Exception:
        LOGGER.exception("audio_input_relay: stream drain raised")


@audio_router.post("/audio-in")
async def receive_device_audio(request: Request) -> dict:
    """device 主導 listen の Ogg/Opus 音声を受信して占有ペルソナに注入。

    Auth: ``Authorization: Bearer <master_token>``
    Content-Type: ``audio/ogg``
    Body: Ogg/Opus payload (= stackchan-mcp gateway の audio_input_hook
        がパック済み、 RFC 7845 準拠)
    Optional header: ``X-StackChan-Session`` (gateway WebSocket session ID、
        ログ・トレース用に metadata に転記)

    Returns:
        202-equivalent ``{"status": "accepted", ...}``。 ペルソナの応答は
        Building の通常 streaming 経路で別途返される。
    """
    # --- 認証 ---
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
        )
    token = auth[len("Bearer "):].strip()

    vessel_manager = get_vessel_manager()
    vessel = vessel_manager.verify_token(token)
    if vessel is None:
        LOGGER.warning("audio_input_relay: token not recognized (no vessel match)")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )

    # --- Body 検証 ---
    body = await request.body()
    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Empty body",
        )
    if not body.startswith(b"OggS"):
        LOGGER.warning(
            "audio_input_relay: body does not look like Ogg "
            "(first 4 bytes=%r, length=%d)",
            body[:4], len(body),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Body is not a valid Ogg container",
        )

    # --- ファイル保存 ---
    try:
        dest_path, dest_name = _save_ogg_capture(body)
    except OSError as exc:
        LOGGER.warning("audio_input_relay: failed to save audio: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save audio",
        )

    session_id = request.headers.get("X-StackChan-Session", "")
    LOGGER.info(
        "audio_input_relay: received %d bytes, saved as %s "
        "(vessel=%s building=%s persona=%s gw_session=%s)",
        len(body), dest_name, vessel.vessel_id,
        vessel.bound_building_id, vessel.bound_persona_id, session_id,
    )

    # --- Vessel Building が無い場合 ---
    # ペアリング済み vessel が存在しても bound_building_id が空のケース。
    # 設定漏れ運用 (Phase 2' のペアリング UI 完成前の暫定運用) を想定。
    if not vessel.bound_building_id:
        LOGGER.warning(
            "audio_input_relay: vessel=%s has no bound_building_id; "
            "audio saved but not injected", vessel.vessel_id,
        )
        return {
            "status": "saved_without_injection",
            "filename": dest_name,
            "bytes": len(body),
            "reason": "vessel has no bound_building_id",
        }

    # --- Manager 取得 ---
    manager = get_manager()
    if manager is None:
        LOGGER.warning("audio_input_relay: manager not available")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SAIVerseManager not ready",
        )

    # --- handle_user_input_stream への注入 ---
    metadata = {
        "source": "stackchan_voice",
        "vessel_id": vessel.vessel_id,
        "gateway_session_id": session_id,
        "media": [
            {
                "type": "audio",
                "uri": f"saiverse://audio/{dest_name}",
                "mime_type": "audio/ogg",
                "path": str(dest_path),
                "source": "stackchan_voice",
            }
        ],
    }

    # 本文は metadata.media[] の Ogg/Opus が主で、 ペルソナ (Gemini) は
    # inline_data で「音」を直接理解する。 ただし SAIVerse 本体
    # (manager/runtime.py の handle_user_input_stream) は空 text を
    # 「入力が空でした」 として弾くガードを持つので、 音声受信を示す
    # 短い system 文をユーザーメッセージとして同送する (memory
    # feedback_system_tag_design.md: role='user' + <system>...</system>
    # 形式)。 これで本体ガードを通過しつつ、 ペルソナには「ｽﾀｯｸﾁｬﾝ経由で
    # 音声が届いた」 という来歴も伝わる。
    audio_intro_text = (
        "<system>ｽﾀｯｸﾁｬﾝから音声入力を受信しました。"
        "添付の音声を聴いて応答してください。</system>"
    )

    try:
        stream = manager.handle_user_input_stream(
            audio_intro_text,
            metadata=metadata,
            building_id=vessel.bound_building_id,
        )
    except Exception as exc:
        LOGGER.exception(
            "audio_input_relay: failed to start user input stream: %s", exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to inject input",
        )

    # generator を background thread で drain する。 本 endpoint は即
    # ``accepted`` を返し、 ペルソナの応答は Building の通常 streaming で
    # 別途ユーザーに届く。
    asyncio.create_task(asyncio.to_thread(_drain_stream_sync, stream))

    # last_seen 更新 (= Vessel が動いていることを記録)
    try:
        vessel_manager.update_last_seen(vessel.vessel_id)
    except Exception as exc:
        # 最終的に致命的じゃないので warning にとどめる。
        LOGGER.warning(
            "audio_input_relay: update_last_seen failed for %s: %s",
            vessel.vessel_id, exc,
        )

    return {
        "status": "accepted",
        "filename": dest_name,
        "bytes": len(body),
        "building_id": vessel.bound_building_id,
    }
