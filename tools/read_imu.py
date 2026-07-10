"""Read the current Stack-chan vessel's on-board 9-axis IMU snapshot."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

from tools.core import ToolSchema

_ADDON_ROOT = str(Path(__file__).resolve().parent.parent)
if _ADDON_ROOT not in sys.path:
    sys.path.insert(0, _ADDON_ROOT)

from vessel_dispatch import (  # noqa: E402
    building_gate_or_hidden,
    list_vessel_building_ids,
    resolve_vessel_connection,
)

LOGGER = logging.getLogger(__name__)

MCP_TOOL_READ_IMU = "read_imu"
_DEFAULT_TIMEOUT_SEC = 15.0


async def _call_read_imu() -> str:
    """Route the raw MCP call to the vessel selected for this persona."""
    _vessel, connection = resolve_vessel_connection()
    return await connection.call_tool(MCP_TOOL_READ_IMU, {})


def _run_on_mcp_loop(coro, timeout_sec: float = _DEFAULT_TIMEOUT_SEC) -> str:
    """Bridge from a synchronous native tool to the MCP client's loop."""
    import tools.mcp_client as mcp_client

    loop = mcp_client._loop
    if loop is None:
        coro.close()
        raise RuntimeError("MCP event loop is not initialized")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout_sec)


def read_imu() -> str:
    """Return acceleration, angular velocity, and magnetic field readings."""
    try:
        rendered = _run_on_mcp_loop(_call_read_imu())
    except Exception as exc:
        LOGGER.exception("read_imu: MCP call failed")
        return f"IMUの値を取得できませんでした: {exc}"

    try:
        payload = json.loads(rendered)
    except (json.JSONDecodeError, TypeError):
        LOGGER.warning("read_imu: gateway returned non-JSON payload")
        return rendered

    if isinstance(payload, dict) and payload.get("ok") is False:
        error = payload.get("error", "unknown IMU error")
        LOGGER.warning("read_imu: device returned error: %s", error)
        return f"IMUの値を取得できませんでした: {error}"

    LOGGER.info("read_imu: collected a 9-axis snapshot from the current vessel")
    return json.dumps(payload, ensure_ascii=False)


def schema() -> ToolSchema:
    building_ids, visible = building_gate_or_hidden(list_vessel_building_ids())
    if not visible:
        LOGGER.info(
            "read_imu: no vessel registered yet; tool hidden everywhere until pairing."
        )
    return ToolSchema(
        name="read_imu",
        description=(
            "現在のStack-chan機体が感じている9軸IMUの値を1回取得する。"
            "加速度(accel_g)、角速度(gyro_dps)、磁場(mag_ut)を、"
            "それぞれx/y/z軸で返す。上下や傾き、動かされた方向を確認したい時に使う。"
            "磁気センサは近くのサーボ磁石の影響を受けるため、方位の断定には使わない。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        result_type="string",
        spell=True,
        spell_display_name="姿勢と動きを感じる",
        spell_visible=visible,
        building_ids=building_ids,
    )
