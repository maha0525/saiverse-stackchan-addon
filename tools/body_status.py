"""``body_status`` ― Stack-chan の身体状態を一括取得する。

生 MCP ツール ``get_device_info`` (バッテリー / 音量 / 輝度 / ネットワーク
等)、 ``get_head_angles`` (首の yaw / pitch)、 ``get_touch_state`` (頭部
タッチセンサー) の 3 つを 1 回のスペルでまとめて呼び、 結果を 1 つの
テキストに統合して返す。 ペルソナが状態確認のたびに 3 スペルを順番に
叩く必要をなくす。

3 つの生ツールは mcp_servers.json で visible=false にして隠し、 ペルソナ
にはこの統合スペルだけを見せる (see ↔ take_photo / move_head と同じ構図)。

device からの返却 JSON のフィールド名は firmware 実装依存なので、 特定の
キーを仮定せず汎用的に「キー: 値」 へ整形する (firmware 変更耐性)。 1 つの
サブ取得が失敗しても残りは返す (部分結果優先)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from tools.core import ToolSchema

# addon root (vessel_dispatch.py) を import 可能にする。詳細は move_head.py の
# 同コメント参照。
_ADDON_ROOT = str(Path(__file__).resolve().parent.parent)
if _ADDON_ROOT not in sys.path:
    sys.path.insert(0, _ADDON_ROOT)
from vessel_dispatch import (  # noqa: E402
    VesselNotAvailable,
    building_gate_or_hidden,
    effective_units,
    list_vessel_building_ids,
    resolve_vessel,
    resolve_vessel_connection,
)

LOGGER = logging.getLogger(__name__)

# ユニット type → ペルソナ向け表示名。 搭載ユニットセクション (body_status に
# 配置を見せる、 docs/intent/stackchan_unit_placement.md §6.1) で使う。 ペルソナ
# は channel 等の物理配線でなく「何が付いていて、 どのラベルで呼べるか」を知り
# たいので、 表示名 + ラベルだけを見せる。
_UNIT_DISPLAY = {
    "env3": "環境センサー (ENV III、 温湿度・気圧)",
    "servo8": "8Servos サーボユニット",
    "sonic": "超音波距離センサー",
    "tof": "ToF 距離センサー (レーザー測距)",
}

ADDON_NAME = "saiverse-stackchan-addon"
MCP_QUALIFIED_SERVER = f"{ADDON_NAME}__stackchan"

_DEFAULT_TIMEOUT_SEC = 15.0

# (生 MCP ツール名, 見出し) の並び。 取得順 = 表示順。
_SECTIONS = [
    ("get_device_info", "デバイス情報"),
    ("get_head_angles", "首の角度"),
    ("get_touch_state", "タッチ状態"),
]

# タッチセンサーがユーザー設定で OFF の時に「タッチ状態」 セクションへ出す
# 案内文。 firmware の gate は HandleTap / HandleStroke (= イベント応答) だけ
# で、 get_touch_state の生クエリは素通りするため、 OFF 中に生値を見せると
# 「なでられてる?」 と紛らわしい。 そこで OFF の時は生クエリを省いて、
# 「いま触覚を切ってある」 ことをペルソナに正しく伝える (= アドオン管理 UI
# の頭タッチセンサートグルと連動)。
_TOUCH_DISABLED_MESSAGE = (
    "タッチセンサーは現在 OFF に設定されています（ユーザー設定）。"
    "頭をなでても検出・反応しません。"
)


async def _call_all() -> dict[str, str]:
    """3 つの生 MCP ツールを順に呼び、 ``{tool_name: rendered_str}`` を返す。

    1 つが失敗しても残りは取りに行く。 失敗したツールは値を
    ``"(取得失敗: ...)"`` にして欠損が分かるようにする。
    """
    _vessel, conn = resolve_vessel_connection()

    # タッチセンサーの有効状態を先に確認する。 OFF (= False) なら
    # get_touch_state の生クエリは意味がない & 紛らわしいので省略し、
    # 案内文に差し替える。 取得失敗 / 不明 (None) の時は従来どおり
    # get_touch_state を呼んで best effort で返す。
    touch_enabled: Optional[bool] = None
    try:
        raw_enabled = await conn.call_tool("get_touch_sensor_enabled", {})
        parsed_enabled = (
            json.loads(raw_enabled) if isinstance(raw_enabled, str)
            else raw_enabled
        )
        if (
            isinstance(parsed_enabled, dict)
            and isinstance(parsed_enabled.get("enabled"), bool)
        ):
            touch_enabled = parsed_enabled["enabled"]
    except Exception as exc:
        LOGGER.warning(
            "body_status: get_touch_sensor_enabled failed: %s", exc,
        )

    results: dict[str, str] = {}
    for tool_name, _heading in _SECTIONS:
        if tool_name == "get_touch_state" and touch_enabled is False:
            results[tool_name] = _TOUCH_DISABLED_MESSAGE
            continue
        try:
            results[tool_name] = await conn.call_tool(tool_name, {})
        except Exception as exc:
            LOGGER.warning("body_status: %s failed: %s", tool_name, exc)
            results[tool_name] = f"(取得失敗: {exc})"
    return results


def _run_on_mcp_loop(coro, timeout_sec: float = _DEFAULT_TIMEOUT_SEC):
    """Bridge from a sync tool to the MCP client's event loop."""
    import tools.mcp_client as _mcp

    loop = _mcp._loop
    if loop is None:
        # 未スケジュールの coroutine を閉じて "never awaited" 警告を防ぐ。この分岐は
        # MCP 未起動時のみ通る (schedule 後は loop 所有なので coro には触らない)。
        coro.close()
        raise RuntimeError("MCP event loop is not initialized")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout_sec)


def _format_value(value: Any) -> str:
    """単一値を表示用文字列にする。 ネストした dict/list はコンパクト JSON。"""
    if isinstance(value, bool):
        return "はい" if value else "いいえ"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _render_section(rendered: str) -> str:
    """生ツールの返却文字列を「キー: 値」 の整形テキストにする。

    JSON dict ならキーごとに 1 行。 ``error`` キーがあれば 1 行で表現。
    JSON でない / dict でない場合は生文字列をそのまま使う (firmware 変更や
    旧版互換のための保険)。
    """
    try:
        parsed = json.loads(rendered)
    except (json.JSONDecodeError, TypeError):
        return rendered.strip()

    if isinstance(parsed, dict):
        if "error" in parsed:
            return f"エラー: {parsed['error']}"
        if not parsed:
            return "(データなし)"
        return "\n".join(
            f"  {key}: {_format_value(val)}" for key, val in parsed.items()
        )
    return _format_value(parsed)


def _render_units_section() -> str:
    """現在 vessel の搭載ユニット (配置) をペルソナ向けテキストにする。

    unit type ごとにまとめ、 ラベルがあれば併記する (channel 等の物理配線の
    詳細は出さない。 ペルソナはラベルで呼ぶ)。 これによりペルソナは自分の身体
    に何が付いていて、 ``get_tof_distance(target=...)`` 等でどのラベルを指定
    できるかを知る (docs/intent/stackchan_unit_placement.md §6.1)。 身体に降り
    ていない / 機体未解決なら空文字 (セクション省略)。
    """
    try:
        vessel = resolve_vessel()
    except VesselNotAvailable:
        return ""
    units = effective_units(vessel)
    if not units:
        return "  (搭載ユニットなし)"

    by_type: dict[str, list[str]] = {}
    order: list[str] = []
    for u in units:
        t = u["type"]
        if t not in by_type:
            by_type[t] = []
            order.append(t)
        by_type[t].append(u["label"])  # 空ラベルも個数カウント用に入れる

    lines: list[str] = []
    for t in order:
        name = _UNIT_DISPLAY.get(t, t)
        labels = [lb for lb in by_type[t] if lb]
        count = len(by_type[t])
        if labels:
            lines.append(f"  {name}: {' / '.join(labels)}")
        elif count > 1:
            lines.append(f"  {name} ×{count}")
        else:
            lines.append(f"  {name}")
    return "\n".join(lines)


def body_status() -> str:
    """Stack-chan の身体状態 (デバイス情報・首角度・タッチ状態・搭載ユニット) を
    一括取得する。

    Returns:
        各セクションを見出し付きで連結した客観テキスト。
    """
    try:
        results = _run_on_mcp_loop(_call_all())
    except Exception as exc:
        LOGGER.exception("body_status: MCP call failed")
        return f"身体の状態を取得できなかった: {exc}"

    lines: list[str] = []
    for tool_name, heading in _SECTIONS:
        rendered = results.get(tool_name, "(取得なし)")
        lines.append(f"【{heading}】")
        lines.append(_render_section(rendered))

    # 搭載ユニット (配置) は vessels.db から (MCP 不要)。 ペルソナがラベルを
    # 知るための発見面 (§6.1)。
    units_text = _render_units_section()
    if units_text:
        lines.append("【搭載ユニット】")
        lines.append(units_text)

    LOGGER.info("body_status: collected sections + units")
    return "\n".join(lines)


def schema() -> ToolSchema:
    # 共通身体ツールは全 Vessel Building で visible (intent 不変条件 #14
    # 共通ツール側)。機体未登録なら building_gate_or_hidden がセンチネル +
    # spell_visible=False に倒す。
    building_ids, visible = building_gate_or_hidden(list_vessel_building_ids())
    if not visible:
        LOGGER.info(
            "body_status: no vessel registered yet; tool hidden everywhere until pairing."
        )
    return ToolSchema(
        name="body_status",
        description=(
            "Stack-chan の身体の状態をまとめて確認する。"
            " デバイス情報 (バッテリー・音量・画面輝度・ネットワーク等)、"
            " 首の角度 (yaw / pitch)、 頭部のタッチ状態を一度に取得して返す。"
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        result_type="string",
        spell=True,
        spell_display_name="身体の状態を確認",
        spell_visible=visible,
        building_ids=building_ids,
    )
