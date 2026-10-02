"""機体 gateway のライフサイクル管理 (intent stackchan_vessel.md 設計 K-2,
Phase 7')。

A-2 方式: 機体ごとに別ポートの gateway subprocess を立てる。gateway は本体
MCP client の名前付きインスタンス (instance_key =
{ADDON}__stackchan:instance:{vessel_id}) として ``register_instance`` で
起動する。token は全機体共通 (master_token)、 機体の区別はポート (各 gateway
別ポート) で成立する。 register_instance の context には vessel_id / ws_port /
capture_port を渡し、 mcp_servers.json の ``${instance.*}`` で解決される。

**常時接続モデル (persistent)**: gateway は「ペアリング済み機体ごとに、
SAIVerse 稼働中はずっと起動」する。 ペルソナの在室有無には連動しない。 理由:
(1) 機体設定 (音量など gateway_config) はペルソナが降りていなくても触れて当然、
(2) 入室のたびに subprocess 起動を待たされる体験を避ける。 起動契機は 3 つ —
起動時 reconcile (全ペアリング機体) / ペアリング直後 (pair_vessel) / 入室時の
冪等な保険 (on_persona_entered_building)。 停止はペアリング解除時 (delete_vessel)
のみ。 退室では止めない (= 旧 lazy モデルの stop-on-exit を廃止。 退室時の
表情消しと gateway 停止が同一イベントでレースし、 停止済み gateway 宛の
set_avatar が timeout→auto-reconnect で孤児 subprocess を生む退行を潰す)。

入退室・pair/delete フックは本体の ThreadPoolExecutor から別スレッドで呼ばれる
ため、 ``persona_context`` (tools.context) は設定されていない。 よって
building_id / vessel を引数から受け取り、 ``get_vessel_by_building`` で機体を
引く (context ベースの resolve_vessel は使わない)。
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
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


def start_vessel_gateway(vessel: Any) -> None:
    """ペアリング直後などに、 機体の gateway を即時起動する (idempotent)。

    常時接続モデルでは、 ペアリングした瞬間から機体設定 (音量など) を触れる
    ように gateway を立てておく。 ペルソナ入室を待たない。
    """
    _register_gateway_for_vessel(vessel)


def stop_vessel_gateway(vessel_id: str) -> None:
    """ペアリング解除時に機体の gateway を停止する (delete_vessel から呼ぶ)。

    常時接続モデルでは、 gateway を止めるのはここだけ (退室では止めない)。
    """
    _stop_gateway_for_vessel(vessel_id)


# ============================================================
# 起動時 reconcile (全ペアリング機体の gateway 起動)
# ============================================================
# 常時接続モデルでは、 SAIVerse 起動時にペアリング済みの全機体の gateway を
# 立てる (在室有無に依らない)。 移動イベント (persona_entered_building) は
# 起動時点で既に在室しているペルソナには発火しないので、 入室フック任せだと
# 再起動を跨いだ機体の gateway が立たない (memory
# feedback_persona_permanent_state_in_registration_hook: Active/移動 経路は
# 再起動を跨げない)。 起動時に 1 回、 全機体の gateway を冪等に起動して埋める。

_STARTUP_RECONCILE_THREAD_NAME = "saiverse-vessel-gateways-startup-reconcile"
_STARTUP_MAX_WAIT_SEC = 60.0
_STARTUP_POLL_INTERVAL_SEC = 1.0


def _reconcile_gateways_on_startup() -> None:
    """起動時に、 ペアリング済み全機体の gateway を起動する (常時接続モデル)。

    MCP manager の初期化 (= main.py startup で addon ロード後に走る) を待って
    から、 ポートが割り当たっている全 vessel の gateway を冪等に起動する。
    """
    from tools import mcp_client as mcp_mod

    deadline = time.monotonic() + _STARTUP_MAX_WAIT_SEC
    while time.monotonic() < deadline:
        if mcp_mod.get_mcp_manager() is not None and mcp_mod._loop is not None:
            break
        time.sleep(_STARTUP_POLL_INTERVAL_SEC)
    else:
        LOGGER.warning(
            "vessel_gateways: MCP manager not ready within %.0fs; "
            "startup reconcile skipped", _STARTUP_MAX_WAIT_SEC,
        )
        return

    try:
        from vessel_manager import get_vessel_manager

        vessels = get_vessel_manager().list_vessels()
    except Exception:
        LOGGER.exception("vessel_gateways: startup reconcile: list_vessels failed")
        return

    for vessel in vessels:
        if vessel.ws_port is None or vessel.capture_port is None:
            LOGGER.warning(
                "vessel_gateways: startup reconcile — vessel %s has no ports; "
                "skip", vessel.vessel_id,
            )
            continue
        LOGGER.info(
            "vessel_gateways: startup reconcile — starting gateway for "
            "vessel %s (building=%s)",
            vessel.vessel_id, getattr(vessel, "bound_building_id", None),
        )
        try:
            _register_gateway_for_vessel(vessel)
        except Exception:
            LOGGER.exception(
                "vessel_gateways: startup reconcile failed for vessel=%s",
                vessel.vessel_id,
            )


def _start_startup_reconcile_thread() -> None:
    """module ロード時に起動時 reconcile daemon thread を 1 つだけ起動する。

    プロセス内で同名 thread が既に走っていれば skip (= 多重 import 対策、
    avatar_loader の ``_start_reconcile_thread`` と同方針)。
    """
    existing = {t.name for t in threading.enumerate() if t.is_alive()}
    if _STARTUP_RECONCILE_THREAD_NAME in existing:
        return
    threading.Thread(
        target=_reconcile_gateways_on_startup,
        name=_STARTUP_RECONCILE_THREAD_NAME,
        daemon=True,
    ).start()


_start_startup_reconcile_thread()


__all__ = [
    "on_persona_entered_building",
    "start_vessel_gateway",
    "stop_vessel_gateway",
]
