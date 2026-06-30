"""ペルソナの Vessel Building 入退室で機体 gateway を起動 / 停止する
(intent stackchan_vessel.md 設計 K-2, Phase 7')。

A-2 方式: 機体ごとに別ポートの gateway subprocess を立てる。ペルソナが
Vessel Building に降りた瞬間にその機体の gateway を本体 MCP client の
名前付きインスタンス (instance_key = {ADDON}__stackchan:instance:{vessel_id})
として ``register_instance`` で起動し、 退室で ``stop_instance`` する。

token は全機体共通 (master_token)、 機体の区別はポート (各 gateway 別ポート)
で成立する。 register_instance の context には vessel_id / ws_port /
capture_port を渡し、 mcp_servers.json の ``${instance.*}`` で解決される。

server_hooks では本ハンドラを avatar_loader より前に並べ、 表情同期 (C) より
先に gateway を立てる。 入退室フックは本体の ThreadPoolExecutor から別スレッド
で呼ばれるため、 ``persona_context`` (tools.context) は設定されていない。
よって building_id を引数から受け取り、 ``get_vessel_by_building`` で機体を
引く (context ベースの resolve_vessel は使わない)。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"
MCP_QUALIFIED_SERVER = f"{ADDON_NAME}__stackchan"

_OP_TIMEOUT_SEC = 40.0


def _register_gateway_for_vessel(vessel: Any) -> None:
    """vessel の gateway インスタンスを起動する (idempotent)。

    subprocess 起動 + device 接続には数秒かかる。入室直後すぐに身体ツールを
    撃つと gateway 起動完了前で ``VesselNotAvailable`` になりうるが、 device の
    再接続ループと register の idempotency で最終的に収束する。
    """
    from tools import mcp_client as mcp_mod

    mcp = mcp_mod.get_mcp_manager()
    if mcp is None:
        LOGGER.warning("vessel_gateways: MCP manager not initialized")
        return
    if vessel.ws_port is None or vessel.capture_port is None:
        LOGGER.warning(
            "vessel_gateways: vessel %s has no ports assigned; skip start",
            vessel.vessel_id,
        )
        return
    loop = mcp_mod._loop
    if loop is None:
        LOGGER.warning("vessel_gateways: MCP event loop not initialized")
        return

    context = {
        "vessel_id": vessel.vessel_id,
        "ws_port": str(vessel.ws_port),
        "capture_port": str(vessel.capture_port),
    }
    future = asyncio.run_coroutine_threadsafe(
        mcp.register_instance(MCP_QUALIFIED_SERVER, vessel.vessel_id, context),
        loop,
    )
    try:
        instance_key = future.result(timeout=_OP_TIMEOUT_SEC)
    except Exception:
        LOGGER.exception(
            "vessel_gateways: failed to start gateway for vessel=%s",
            vessel.vessel_id,
        )
        return
    LOGGER.info(
        "vessel_gateways: gateway started vessel=%s instance=%s "
        "ws_port=%s capture_port=%s",
        vessel.vessel_id, instance_key, vessel.ws_port, vessel.capture_port,
    )


def _stop_gateway_for_vessel(vessel_id: str) -> None:
    from tools import mcp_client as mcp_mod

    mcp = mcp_mod.get_mcp_manager()
    if mcp is None:
        return
    loop = mcp_mod._loop
    if loop is None:
        return
    instance_key = mcp_mod._make_instance_key(
        MCP_QUALIFIED_SERVER, instance_id=vessel_id
    )
    future = asyncio.run_coroutine_threadsafe(
        mcp.stop_instance(instance_key), loop
    )
    try:
        future.result(timeout=_OP_TIMEOUT_SEC)
    except Exception:
        LOGGER.exception(
            "vessel_gateways: failed to stop gateway for vessel=%s", vessel_id
        )
        return
    LOGGER.info("vessel_gateways: gateway stopped vessel=%s", vessel_id)


def _vessel_for_building(building_id: str) -> Optional[Any]:
    from vessel_manager import get_vessel_manager

    return get_vessel_manager().get_vessel_by_building(building_id)


def on_persona_entered_building(
    persona_id: str,
    building_id: str,
    **_kwargs,
) -> None:
    """Vessel Building 入室で、 その機体の gateway を起動する。

    Vessel Building でない / 未ペアリングの building は何もしない。例外は
    握り潰す (= ペルソナ移動経路を gateway の都合で壊さない、 avatar_loader と
    同方針)。
    """
    try:
        vessel = _vessel_for_building(building_id)
        if vessel is None:
            return
        LOGGER.info(
            "vessel_gateways: persona=%s entered vessel building=%s -> "
            "starting gateway vessel=%s",
            persona_id, building_id, vessel.vessel_id,
        )
        _register_gateway_for_vessel(vessel)
    except Exception:
        LOGGER.exception(
            "vessel_gateways: on_persona_entered_building failed "
            "(persona=%s building=%s)",
            persona_id, building_id,
        )


def on_persona_exited_building(
    persona_id: str,
    building_id: str,
    **_kwargs,
) -> None:
    """Vessel Building 退室で、 その機体の gateway を停止する。

    capacity=1 なので退室すれば誰も使っていない。 B 段階では入退室に同期させる
    (= 身体に降りている間だけ身体が起きる、 認知モデルと一致)。device の常時
    接続を保ちたくなったら将来この停止を外す選択肢もある。
    """
    try:
        vessel = _vessel_for_building(building_id)
        if vessel is None:
            return
        LOGGER.info(
            "vessel_gateways: persona=%s exited vessel building=%s -> "
            "stopping gateway vessel=%s",
            persona_id, building_id, vessel.vessel_id,
        )
        _stop_gateway_for_vessel(vessel.vessel_id)
    except Exception:
        LOGGER.exception(
            "vessel_gateways: on_persona_exited_building failed "
            "(persona=%s building=%s)",
            persona_id, building_id,
        )


__all__ = [
    "on_persona_entered_building",
    "on_persona_exited_building",
]
