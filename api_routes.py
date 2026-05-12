"""Stack-chan Vessel アドオン: FastAPI ルート定義。

addon_loader が ``/api/addon/saiverse-stackchan-addon`` プレフィックスで自動マウントする。

エンドポイント:
    WebSocket  /vessel               Stack-chan device の接続用
    POST       /pair                 新規ペアリング発行
    GET        /vessels              登録済み vessel 一覧と接続状態
    DELETE     /vessels/{vessel_id}  ペアリング解除

詳細プロトコル: docs/intent/stackchan_vessel.md
"""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from saiverse.addon_deps import get_manager

# addon_loader は spec_from_file_location で api_routes.py をロードするため、
# このモジュールには __package__ が設定されず ``from .vessel_manager`` のような
# 相対 import が動かない。同梱の vessel_manager.py を絶対 import するために、
# パック自身のディレクトリを sys.path に追加する。
_PACK_DIR = str(Path(__file__).parent)
if _PACK_DIR not in sys.path:
    sys.path.insert(0, _PACK_DIR)

from vessel_manager import VesselSession, get_vessel_manager  # noqa: E402

LOGGER = logging.getLogger(__name__)

router = APIRouter()

_ADDON_DIR = Path(__file__).parent


# ============================================================================
# Static: Web Serial フラッシュ用 setup ページ + ファームウェアバイナリ配信
# ============================================================================

@router.get("/setup", response_class=HTMLResponse)
async def setup_ui_page() -> HTMLResponse:
    """Web Serial による esptool-js フラッシュ用静的 HTML を返す。

    AddonManager UI の Panel.tsx から `window.open(addonApiBase + '/setup')`
    で開かれる。ユーザーは Chrome / Edge ブラウザで Stack-chan を USB 接続し、
    このページからファームウェアを書き込む。
    """
    html_path = _ADDON_DIR / "setup_ui" / "index.html"
    if not html_path.exists():
        return HTMLResponse(
            "<h1>setup_ui not bundled</h1>"
            "<p>setup_ui/index.html がアドオンに同梱されていません。</p>",
            status_code=404,
        )
    return HTMLResponse(html_path.read_text(encoding="utf-8"))


@router.get("/firmware.bin")
async def firmware_binary() -> Response:
    """配布用ファームウェアバイナリ。setup ページから fetch されて
    esptool-js で書き込まれる。

    ``firmware/dist/firmware.bin`` (PlatformIO ビルド成果物) があればそれを返す。
    無ければ 404 + プレーンテキスト案内 (まだビルドされていない開発初期段階用)。
    """
    bin_path = _ADDON_DIR / "firmware" / "dist" / "firmware.bin"
    if not bin_path.exists():
        return Response(
            content=(
                "firmware.bin not bundled. "
                "Run `pio run` in expansion_data/saiverse-stackchan-addon/firmware/ "
                "and copy .pio/build/m5stack-cores3/firmware.bin to firmware/dist/."
            ),
            status_code=404,
            media_type="text/plain",
        )
    return FileResponse(bin_path, media_type="application/octet-stream")


# ============================================================================
# HTTP API: Pairing / management
# ============================================================================

class PairRequest(BaseModel):
    building_id: str
    hardware_model: str = "stackchan_ai_desktop_v1"


class PairResponse(BaseModel):
    vessel_id: str
    device_token: str  # 平文、ペアリング時に 1 回だけ返される
    building_id: str


class VesselSummary(BaseModel):
    vessel_id: str
    building_id: str
    hardware_model: str
    firmware_version: Optional[str]
    paired_at: str
    last_seen_at: Optional[str]
    connected: bool


@router.post("/pair", response_model=PairResponse)
def pair_vessel(req: PairRequest, manager=Depends(get_manager)) -> PairResponse:
    """新規ペアリング発行。

    1. Building が存在することを確認
    2. その Building が他の vessel に既にペア済みでないことを確認
    3. vessel_id + device_token を発行
    4. Building.PHYSICAL_VESSEL_ID に vessel_id をセット、CAPACITY=1 強制

    device_token は平文で 1 回だけレスポンスに含まれる。DB には sha256 ハッシュ
    のみが保存されるため、紛失時は再ペアリングが必要。
    """
    from database.models import Building

    db = manager.SessionLocal()
    try:
        building = db.query(Building).filter_by(BUILDINGID=req.building_id).first()
        if not building:
            raise HTTPException(
                status_code=404,
                detail=f"Building '{req.building_id}' not found",
            )
        if building.PHYSICAL_VESSEL_ID:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Building '{req.building_id}' is already paired with vessel "
                    f"'{building.PHYSICAL_VESSEL_ID}'. Unpair first."
                ),
            )

        vm = get_vessel_manager()
        vessel_id, device_token = vm.create_pairing(
            building_id=req.building_id,
            hardware_model=req.hardware_model,
        )

        building.PHYSICAL_VESSEL_ID = vessel_id
        # Vessel Building は capacity=1 強制 (Intent Doc 不変条件 2)
        building.CAPACITY = 1
        db.commit()

        LOGGER.info(
            "pair_vessel: building_id=%s <-> vessel_id=%s paired",
            req.building_id, vessel_id,
        )
        return PairResponse(
            vessel_id=vessel_id,
            device_token=device_token,
            building_id=req.building_id,
        )
    finally:
        db.close()


@router.get("/vessels")
def list_vessels() -> Dict[str, Any]:
    """登録済み vessel 一覧と接続状態を返す。"""
    vm = get_vessel_manager()
    records = vm.list_vessels()
    connected_ids = {s.vessel_id for s in vm.list_sessions()}

    return {
        "vessels": [
            VesselSummary(
                vessel_id=r.vessel_id,
                building_id=r.building_id,
                hardware_model=r.hardware_model,
                firmware_version=r.firmware_version,
                paired_at=r.paired_at,
                last_seen_at=r.last_seen_at,
                connected=r.vessel_id in connected_ids,
            ).model_dump()
            for r in records
        ]
    }


@router.delete("/vessels/{vessel_id}")
async def delete_vessel(vessel_id: str, manager=Depends(get_manager)) -> Dict[str, bool]:
    """ペアリング解除。Building.PHYSICAL_VESSEL_ID を NULL に戻し、接続中なら WS を閉じる。"""
    from database.models import Building

    vm = get_vessel_manager()
    records = vm.list_vessels()
    target = next((r for r in records if r.vessel_id == vessel_id), None)
    if not target:
        raise HTTPException(status_code=404, detail=f"Vessel '{vessel_id}' not found")

    # Building.PHYSICAL_VESSEL_ID を NULL に戻す
    db = manager.SessionLocal()
    try:
        building = db.query(Building).filter_by(
            BUILDINGID=target.building_id,
            PHYSICAL_VESSEL_ID=vessel_id,
        ).first()
        if building:
            building.PHYSICAL_VESSEL_ID = None
            db.commit()
    finally:
        db.close()

    # 接続中なら WebSocket を閉じる (close 失敗は WARNING ログのみ、削除自体は続行)
    session = vm.get_session(vessel_id)
    if session:
        try:
            await session.ws.close(code=1000, reason="vessel unpaired")
        except Exception:
            LOGGER.warning(
                "delete_vessel: WS close failed for vessel_id=%s (already disconnected?)",
                vessel_id, exc_info=True,
            )

    deleted = vm.delete_vessel(vessel_id)
    LOGGER.info("delete_vessel: vessel_id=%s deleted=%s", vessel_id, deleted)
    return {"deleted": deleted}


# ============================================================================
# WebSocket: Vessel device 接続
# ============================================================================

async def _close_with_error(ws: WebSocket, code: str, reason: str, ws_code: int = 1008) -> None:
    """WebSocket にエラーメッセージを送ってクローズする。"""
    try:
        await ws.send_json({"type": "error", "code": code, "reason": reason})
    except Exception:
        LOGGER.debug("vessel_endpoint: send_json failed during error close (already disconnected?)")
    try:
        await ws.close(code=ws_code, reason=reason)
    except Exception:
        LOGGER.debug("vessel_endpoint: close failed during error close")


@router.websocket("/vessel")
async def vessel_endpoint(ws: WebSocket) -> None:
    """Stack-chan device の WebSocket 接続エンドポイント。

    プロトコル (Phase 1):
        D->S hello {vessel_id, device_token, firmware_version?, capabilities?}
        S->D welcome {vessel_id, bound_building_id}  または
             error {code, reason} + close

        D->S ping {seq}
        S->D pong {seq}

        D->S echo {text}                Phase 1 のテキスト往復確認用
        S->D echo_reply {text}

    Phase 2 以降で audio_chunk / touch / motor 等を追加する。
    """
    await ws.accept()
    vm = get_vessel_manager()
    session: Optional[VesselSession] = None
    vessel_id: Optional[str] = None

    try:
        # --- Step 1: hello メッセージで認証 ---
        try:
            raw = await ws.receive_text()
        except WebSocketDisconnect:
            LOGGER.info("vessel_endpoint: disconnected before hello")
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("vessel_endpoint: invalid JSON for hello: %r", raw[:200])
            await _close_with_error(ws, "invalid_json", "hello must be valid JSON")
            return

        if msg.get("type") != "hello":
            await _close_with_error(ws, "expected_hello", "first message must be type=hello")
            return

        vessel_id = msg.get("vessel_id")
        device_token = msg.get("device_token")
        firmware_version = msg.get("firmware_version")
        if not vessel_id or not device_token:
            await _close_with_error(ws, "auth_required", "vessel_id and device_token are required")
            return

        record = vm.verify_device(vessel_id, device_token)
        if not record:
            await _close_with_error(ws, "auth_failed", "invalid vessel_id or device_token")
            return

        # --- Step 2: 認証成功、セッション登録と welcome 送信 ---
        if firmware_version:
            vm.update_firmware_version(vessel_id, firmware_version)
        vm.update_last_seen(vessel_id)

        session = VesselSession(
            vessel_id=vessel_id,
            building_id=record.building_id,
            ws=ws,
            firmware_version=firmware_version,
        )
        vm.register_session(session)

        await ws.send_json({
            "type": "welcome",
            "vessel_id": vessel_id,
            "bound_building_id": record.building_id,
        })
        LOGGER.info(
            "vessel_endpoint: vessel_id=%s connected (building_id=%s, fw=%s)",
            vessel_id, record.building_id, firmware_version,
        )

        # --- Step 3: メッセージループ ---
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                LOGGER.warning(
                    "vessel_endpoint: invalid JSON from vessel_id=%s: %r",
                    vessel_id, raw[:200],
                )
                continue

            mtype = msg.get("type")
            if mtype == "ping":
                await ws.send_json({"type": "pong", "seq": msg.get("seq")})
                vm.update_last_seen(vessel_id)
            elif mtype == "echo":
                text = msg.get("text", "")
                await ws.send_json({"type": "echo_reply", "text": text})
                vm.update_last_seen(vessel_id)
            else:
                LOGGER.warning(
                    "vessel_endpoint: unknown message type=%r from vessel_id=%s",
                    mtype, vessel_id,
                )

    except WebSocketDisconnect:
        LOGGER.info("vessel_endpoint: vessel_id=%s disconnected", vessel_id)
    except Exception:
        LOGGER.exception("vessel_endpoint: unexpected error vessel_id=%s", vessel_id)
        try:
            await ws.close(code=1011, reason="server error")
        except Exception:
            LOGGER.debug("vessel_endpoint: close failed in error handler")
    finally:
        if vessel_id:
            vm.unregister_session(vessel_id)
