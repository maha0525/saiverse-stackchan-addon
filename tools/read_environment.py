"""Read the current Stack-chan vessel's ambient-light and proximity snapshot."""

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

MCP_TOOL_READ_ENVIRONMENT = "read_environment"
_DEFAULT_TIMEOUT_SEC = 15.0


async def _call_read_environment() -> str:
    """Route the raw MCP call to the vessel selected for this persona."""
    _vessel, connection = resolve_vessel_connection()
    return await connection.call_tool(MCP_TOOL_READ_ENVIRONMENT, {})


def _run_on_mcp_loop(coro, timeout_sec: float = _DEFAULT_TIMEOUT_SEC) -> str:
    """Bridge from a synchronous native tool to the MCP client's loop."""
    import tools.mcp_client as mcp_client

    loop = mcp_client._loop
    if loop is None:
        coro.close()
        raise RuntimeError("MCP event loop is not initialized")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout_sec)


def read_environment() -> str:
    """Return LTR-553 ambient-light and proximity ADC snapshots."""
    try:
        rendered = _run_on_mcp_loop(_call_read_environment())
    except Exception as exc:
        LOGGER.exception("read_environment: MCP call failed")
        return f"環境光・近接センサーの値を取得できませんでした: {exc}"

    try:
        payload = json.loads(rendered)
    except (json.JSONDecodeError, TypeError):
        LOGGER.warning("read_environment: gateway returned non-JSON payload")
        return rendered

    if isinstance(payload, dict) and payload.get("ok") is False:
        error = payload.get("error", "unknown LTR-553 error")
        LOGGER.warning("read_environment: device returned error: %s", error)
        return f"環境光・近接センサーの値を取得できませんでした: {error}"

    LOGGER.info("read_environment: collected an LTR-553 snapshot from the current vessel")
    return json.dumps(payload, ensure_ascii=False)


def schema() -> ToolSchema:
    building_ids, visible = building_gate_or_hidden(list_vessel_building_ids())
    if not visible:
        LOGGER.info(
            "read_environment: no vessel registered yet; tool hidden everywhere until pairing."
        )
    return ToolSchema(
        name="read_environment",
        description=(
            "現在のStack-chan機体が感じている環境光と近接の値を1回取得する。"
            "環境光は可視+IRとIRのみのADC count、近接もADC countで返す。"
            "明るさの変化、手や物が顔の近くにあるかを確かめたい時に使う。"
            "常時監視や距離への換算は行わない。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        result_type="string",
        spell=True,
        spell_display_name="光と近さを感じる",
        spell_visible=visible,
        building_ids=building_ids,
    )
