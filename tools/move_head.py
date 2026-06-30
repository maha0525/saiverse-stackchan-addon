"""``move_head`` ― Stack-chan の首を動かし、サーボが静止するまで待つ。

生 MCP ツール ``move_head`` (stackchan-mcp gateway 経由) をラップする
薄い native tool。 device はコマンドをキューに入れた時点で ack を返す
ため、 生ツールをそのまま呼ぶと首がまだ動いている最中に制御が戻る。
直後に ``see`` (= ``take_photo``) を呼ぶと移動中の首を撮ってブレるので、
ここで命令発行後にサーボの静定時間ぶん待ってから返す。

「動作命令 → 静定待ち → 返却」 は move 動作そのものの責務なので、
撮影側 (see.py) ではなくこのラッパに置く。 upstream gateway は汎用
MCP サーバーであり、 SAIVerse 固有のカメラ事情を持ち込まないために
本体側 (この addon) でラップする。 生 ``move_head`` は mcp_servers.json
で visible=false にして隠し、 ペルソナにはこのラッパだけを見せる
(see ↔ take_photo と同じ構図)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

from tools.core import ToolSchema

# addon root (vessel_dispatch.py / vessel_manager.py と同階層) を import 可能に
# する。 tool loader は addon の tools/ までしか sys.path に積まないため、 1 段上
# を明示的に通す (env3.py が hubs.pahub を import するのと同じ事情)。
_ADDON_ROOT = str(Path(__file__).resolve().parent.parent)
if _ADDON_ROOT not in sys.path:
    sys.path.insert(0, _ADDON_ROOT)
from vessel_dispatch import (  # noqa: E402
    list_vessel_building_ids,
    resolve_vessel_connection,
)

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"
MCP_QUALIFIED_SERVER = f"{ADDON_NAME}__stackchan"
MCP_TOOL_MOVE_HEAD = "move_head"

_DEFAULT_TIMEOUT_SEC = 30.0

# サーボが目標角に達して静止するまでの待ち時間 (秒)。 ブレ具合を見て
# コードを書き換えずに調整できるよう env で上書き可能。 0 で待ちを無効化。
_DEFAULT_SETTLE_SEC = 1.0
_SETTLE_ENV = "STACKCHAN_MOVE_HEAD_SETTLE_S"


def _settle_sec() -> float:
    raw = os.environ.get(_SETTLE_ENV)
    if raw is None:
        return _DEFAULT_SETTLE_SEC
    try:
        return float(raw)
    except ValueError:
        LOGGER.warning(
            "move_head: invalid %s=%r; falling back to %.2fs",
            _SETTLE_ENV,
            raw,
            _DEFAULT_SETTLE_SEC,
        )
        return _DEFAULT_SETTLE_SEC


def _vessel_building_id() -> Optional[str]:
    """AddonConfig から Vessel Building ID を取得する。"""
    try:
        from saiverse.addon_config import get_params

        params = get_params(ADDON_NAME)
        vbid = params.get("vessel_building_id") if params else None
        return str(vbid) if vbid else None
    except Exception:
        LOGGER.exception("move_head: failed to resolve vessel_building_id")
        return None


async def _call_move_head(yaw: int, pitch: int) -> str:
    """Call the raw ``move_head`` MCP tool, then wait for the head to settle."""
    _vessel, conn = resolve_vessel_connection()
    rendered = await conn.call_tool(
        MCP_TOOL_MOVE_HEAD, {"yaw": yaw, "pitch": pitch}
    )

    settle = _settle_sec()
    if settle > 0:
        LOGGER.debug("move_head: waiting %.2fs for head to settle", settle)
        await asyncio.sleep(settle)
    return rendered


def _run_on_mcp_loop(coro, timeout_sec: float = _DEFAULT_TIMEOUT_SEC) -> str:
    """Bridge from a sync tool to the MCP client's event loop."""
    import tools.mcp_client as _mcp

    loop = _mcp._loop
    if loop is None:
        raise RuntimeError("MCP event loop is not initialized")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout_sec)


def move_head(yaw: int = 0, pitch: int = 45) -> str:
    """Stack-chan の首を指定角度に向け、 静止してから結果を返す。

    Args:
        yaw: 水平角 (度)。 -90 〜 90。
        pitch: 垂直角 (度)。 5 〜 85 (M5Stack 推奨可動域)。

    Returns:
        設定結果を表す客観的なテキスト。
    """
    try:
        rendered = _run_on_mcp_loop(_call_move_head(int(yaw), int(pitch)))
    except Exception as exc:
        LOGGER.exception("move_head: MCP call failed")
        return f"首の角度を変更できなかった: {exc}"

    # 生ツールはエラー時 {"error": "..."} を返す。 成功時の payload は
    # firmware 実装依存なので、 error だけ拾って後はそのまま通す。
    try:
        payload = json.loads(rendered)
    except (json.JSONDecodeError, TypeError):
        payload = None

    if isinstance(payload, dict) and "error" in payload:
        LOGGER.warning("move_head: device returned error: %s", payload["error"])
        return f"首の角度を変更できなかった: {payload['error']}"

    LOGGER.info("move_head: set yaw=%s pitch=%s (settled)", yaw, pitch)
    return f"首の角度を設定しました (yaw={yaw}°, pitch={pitch}°)。"


def schema() -> ToolSchema:
    # 共通身体ツールは全 Vessel Building で visible (どの機体に降りても首は
    # 振れる、 intent 不変条件 #14 共通ツール側)。機体未登録なら None = 非表示。
    building_ids = list_vessel_building_ids() or None
    if not building_ids:
        LOGGER.info(
            "move_head: no vessel registered yet; tool hidden until pairing."
        )
    return ToolSchema(
        name="move_head",
        description=(
            "Stack-chan の首を動かして向きを変える。"
            " yaw は水平方向 (-90〜90度)、 pitch は垂直方向 (5〜85度)。"
            " 動作後にサーボが静止するまで待ってから返すので、"
            " 直後に「見る」 を呼んでもブレない。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "yaw": {
                    "type": "integer",
                    "description": "水平角 (度)。 -90 (左) 〜 90 (右)。",
                    "minimum": -90,
                    "maximum": 90,
                },
                "pitch": {
                    "type": "integer",
                    "description": "垂直角 (度)。 5 (下) 〜 85 (上)。 M5Stack 推奨可動域。",
                    "minimum": 5,
                    "maximum": 85,
                },
            },
            "required": ["yaw", "pitch"],
        },
        result_type="string",
        spell=True,
        spell_display_name="首を動かす",
        spell_visible=True,
        building_ids=building_ids,
    )
