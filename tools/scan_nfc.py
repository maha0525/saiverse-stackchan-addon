"""Scan one ISO 14443A or NFC-F tag with the current Stack-chan vessel."""

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

MCP_TOOL_SCAN_NFC = "scan_nfc"
_DEFAULT_TIMEOUT_SEC = 15.0


async def _call_scan_nfc() -> str:
    """Route the raw MCP call to the vessel selected for this persona."""
    _vessel, connection = resolve_vessel_connection()
    return await connection.call_tool(MCP_TOOL_SCAN_NFC, {})


def _run_on_mcp_loop(coro, timeout_sec: float = _DEFAULT_TIMEOUT_SEC) -> str:
    """Bridge from a synchronous native tool to the MCP client's loop."""
    import tools.mcp_client as mcp_client

    loop = mcp_client._loop
    if loop is None:
        coro.close()
        raise RuntimeError("MCP event loop is not initialized")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout_sec)


def scan_nfc() -> str:
    """Return identifier metadata from one explicit, non-destructive NFC scan."""
    try:
        rendered = _run_on_mcp_loop(_call_scan_nfc())
    except Exception as exc:
        LOGGER.exception("scan_nfc: MCP call failed")
        return f"NFCタグをスキャンできませんでした: {exc}"

    try:
        payload = json.loads(rendered)
    except (json.JSONDecodeError, TypeError):
        LOGGER.warning("scan_nfc: gateway returned non-JSON payload")
        return rendered

    if isinstance(payload, dict) and payload.get("ok") is False:
        error = payload.get("error", "unknown ST25R3916 error")
        LOGGER.warning("scan_nfc: device returned error: %s", error)
        return f"NFCタグをスキャンできませんでした: {error}"

    LOGGER.info("scan_nfc: completed one explicit NFC scan for the current vessel")
    return json.dumps(payload, ensure_ascii=False)


def schema() -> ToolSchema:
    building_ids, visible = building_gate_or_hidden(list_vessel_building_ids())
    if not visible:
        LOGGER.info("scan_nfc: no vessel registered yet; tool hidden everywhere until pairing.")
    return ToolSchema(
        name="scan_nfc",
        description=(
            "現在のStack-chan機体で、近くにかざされたISO 14443AまたはNFC-F（FeliCa）タグを1回だけ探す。"
            "ISO 14443AはUID・ATQA・SAK、NFC-FはIDm・PMmを返す。タグ内容の読書き、認証、カードのエミュレーションは行わない。"
            "UIDやIDmは安定した識別子になり得るため、NFCタグの確認を求められた時だけ使う。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        result_type="string",
        spell=True,
        spell_display_name="NFCタグを探す",
        spell_visible=visible,
        building_ids=building_ids,
    )
