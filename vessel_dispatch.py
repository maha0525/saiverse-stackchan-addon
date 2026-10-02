"""現在ペルソナが降りている機体の gateway インスタンスを解決する dispatcher。

全身体ツール (move_head / see / body_status / set_avatar / set_led / ... +
ENV III 等のユニット由来ツール) が共有する。生 MCP ツールをペルソナに直接
見せず、 ペルソナには機体に依らない単一論理名だけを見せたうえで、 実行時に

    現在の Vessel Building → vessel (vessels.db) → 名前付きインスタンス
    ({ADDON}__stackchan:instance:{vessel_id}) の MCP connection

へ転送する (intent stackchan_vessel.md 設計 K-4)。ペルソナは「どの機体か」を
意識しない。リビングの機体に降りていればリビングの身体が、 机に降りていれば
机の身体が動く。

複数機体の同時稼働では、 1 gateway = 1 device の subprocess が機体ごとに
別ポートで起動している (A-2 方式)。本 dispatcher は「いまそのペルソナが居る
Building の機体」へ各ツールコールを振り分ける役目を負う。

import 解決: SAIVerse の tool loader は addon の ``tools/`` 配下を sys.path に
積むだけなので、 addon root (= ``vessel_manager.py`` と同階層) の本モジュールを
``tools/<wrapper>.py`` から import するには、 wrapper 側で addon root を
sys.path に積む必要がある (env3.py が ``hubs.pahub`` を import するのと同じ
事情)。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"
MCP_QUALIFIED_SERVER = f"{ADDON_NAME}__stackchan"


class VesselNotAvailable(RuntimeError):
    """現在のペルソナに対応する稼働中の機体 gateway が無い。

    身体ツールはこれを捕捉して「身体に降りていない / 機体が接続されていない」
    旨の客観メッセージをペルソナに返す。
    """


def _current_persona_and_building() -> Tuple[Optional[str], Optional[str]]:
    """実行文脈のペルソナ ID と、 そのペルソナが現在居る Building ID を返す。

    取得経路は ``tools.mcp_client.check_building_gate`` と同一 (= manager の
    ``all_personas`` / ``personas`` から persona オブジェクトを引き、
    ``current_building_id`` を読む)。
    """
    from tools.context import get_active_manager, get_active_persona_id

    persona_id = get_active_persona_id()
    manager = get_active_manager()
    building_id: Optional[str] = None
    if manager is not None and persona_id:
        persona_obj = (
            getattr(manager, "all_personas", {}).get(persona_id)
            or getattr(manager, "personas", {}).get(persona_id)
        )
        if persona_obj is not None:
            building_id = getattr(persona_obj, "current_building_id", None)
    return persona_id, building_id


def resolve_vessel() -> Any:
    """現在ペルソナが降りている vessel (VesselRecord) を返す。

    解決の優先順位は :func:`resolve_vessel_connection` と揃える:

    1. **対象機体の明示上書き** (contextvar ``get_tool_target_instance_id``):
       addon 管理 UI の複合アクション「テスト実行」は persona 文脈を持たない
       ため、 UI で選んだ機体の vessel_id を core の contextvar 経由で渡す。
       これがあれば persona 文脈より優先し、 その機体を直接返す。 こうしないと、
       capability チェック (各ユニット tool の ``_unit_present``) は persona 文脈
       (テスト実行では空) で解決を試みて ``VesselNotAvailable`` になり、 常時
       ON のユニットまで「搭載されていない」扱いになる。 一方 gateway 接続解決
       (:func:`resolve_vessel_connection`) は上書きを尊重するため、 両者を揃えて
       おかないと「接続は機体 X・capability 判定は失敗」と食い違う。
    2. **persona 文脈** (通常のスペル経路): 現在ペルソナが降りている Building
       から機体を逆引きする。

    Raises:
        VesselNotAvailable: 上書き機体が未登録、 ペルソナ / Building が解決
            できない、 またはその Building に紐付く機体が無い場合。
    """
    from vessel_manager import get_vessel_manager

    # 1. 対象機体の明示上書き (テスト実行) を最優先で解決する。 通常のスペル経路
    #    では None なので下の persona 文脈解決に落ちる。
    from tools.context import get_tool_target_instance_id

    forced_vessel_id = get_tool_target_instance_id()
    if forced_vessel_id:
        vessel = get_vessel_manager().get_vessel(forced_vessel_id)
        if vessel is None:
            raise VesselNotAvailable(
                f"機体 '{forced_vessel_id}' が登録されていません "
                "(テスト対象の機体が削除された可能性)"
            )
        return vessel

    # 2. persona 文脈から解決する (通常のスペル経路)。
    persona_id, building_id = _current_persona_and_building()
    if not persona_id or not building_id:
        raise VesselNotAvailable(
            "現在のペルソナまたは Building を解決できませんでした"
        )
    vessel = get_vessel_manager().get_vessel_for_persona(persona_id, building_id)
    if vessel is None:
        raise VesselNotAvailable(
            f"Building '{building_id}' に紐付く機体がありません"
        )
    return vessel


def resolve_vessel_connection() -> Tuple[Any, Any]:
    """現在の機体の gateway インスタンスへの MCP connection を返す。

    Returns:
        ``(vessel, connection)`` のタプル。connection は
        ``MCPServerConnection`` で、 ``await connection.call_tool(name, args)``
        で生 MCP ツールを叩ける。

    Raises:
        VesselNotAvailable: vessel が解決できない、 または該当機体の gateway
            インスタンスが接続されていない場合。
    """
    # 機体解決 (テスト実行の明示上書き + persona 文脈) は resolve_vessel に一元化
    # 済み。 ここで別途上書きを解釈すると capability チェック (_unit_present) と
    # 経路が分かれて食い違うため、 必ず resolve_vessel を通す。
    vessel = resolve_vessel()

    from tools.mcp_client import _make_instance_key, get_mcp_manager

    mcp = get_mcp_manager()
    if mcp is None:
        raise VesselNotAvailable("MCP manager が初期化されていません")

    instance_key = _make_instance_key(
        MCP_QUALIFIED_SERVER, instance_id=vessel.vessel_id
    )
    conn = mcp._connections.get(instance_key)
    if conn is None:
        raise VesselNotAvailable(
            f"機体 '{vessel.vessel_id}' の gateway が接続されていません "
            f"(instance_key={instance_key})"
        )
    return vessel, conn


def resolve_vessel_connection_for_building(building_id: str) -> Tuple[Any, Any]:
    """指定 building の機体 gateway への MCP connection を返す。

    背景スレッド (avatar_loader の入退室フック・reconcile ループ) は persona
    context を持たないため、 building_id を明示的に渡してこちらを使う
    (context ベースの :func:`resolve_vessel_connection` と対)。

    Returns:
        ``(vessel, connection)`` のタプル。

    Raises:
        VesselNotAvailable: building に紐付く機体が無い、 または該当機体の
            gateway インスタンスが接続されていない場合。
    """
    from vessel_manager import get_vessel_manager

    vessel = get_vessel_manager().get_vessel_by_building(building_id)
    if vessel is None:
        raise VesselNotAvailable(
            f"Building '{building_id}' に紐付く機体がありません"
        )

    from tools.mcp_client import _make_instance_key, get_mcp_manager

    mcp = get_mcp_manager()
    if mcp is None:
        raise VesselNotAvailable("MCP manager が初期化されていません")

    instance_key = _make_instance_key(
        MCP_QUALIFIED_SERVER, instance_id=vessel.vessel_id
    )
    conn = mcp._connections.get(instance_key)
    if conn is None:
        raise VesselNotAvailable(
            f"機体 '{vessel.vessel_id}' の gateway が接続されていません "
            f"(instance_key={instance_key})"
        )
    return vessel, conn


def list_vessel_building_ids() -> list:
    """全 Vessel Building の building_id リスト (共通身体ツールの building_ids 用)。

    全機体が持つ共通ツール (move_head / see / body_status 等) は、 どの Vessel
    Building に降りていても見える必要がある (intent 不変条件 #14 の共通ツール
    側)。 機体ごとに別 Vessel Building なので全機体ぶんを集める。 機体未登録
    なら空リスト (= どこにも出さない、 安全側)。 ユニット由来ツール
    (env3 等) はこれと違い capability を持つ機体だけに絞る (C / ④)。
    """
    from vessel_manager import get_vessel_manager

    ids: list = []
    for v in get_vessel_manager().list_vessels():
        bid = v.bound_building_id
        if bid and bid not in ids:
            ids.append(bid)
    return ids


# 対象機体ゼロのとき building_ids に入れる実在しないセンチネル building_id。
# ``None`` / ``[]`` は ToolSchema 上「制限なし = 全 Building で visible + 実行
# ゲートなし」を意味してしまう (tools/core.py の building_ids コメント参照)。
# そのため ``list_...() or None`` で空を None に倒すと、機体未登録のときに逆に
# ツールが全 Building へ露出する。実在しない building_id を1件入れておくと、
# 可視フィルタ (current_building_id in building_ids) と実行ゲート
# (check_building_gate) の両方が常に外れ、「どの Building にも出さない・どこでも
# 実行不可」に倒せる (安全側)。
_NO_MATCHING_BUILDING = "__stackchan_no_vessel__"


def building_gate_or_hidden(building_ids: list) -> Tuple[list, bool]:
    """身体/ユニットツールの ``building_ids`` と ``spell_visible`` を安全側に確定する。

    ``list_vessel_building_ids`` / ``list_building_ids_with_capability`` の戻り値を
    そのまま渡す。対象機体があればそのリスト + ``spell_visible=True``。空 (対象
    機体ゼロ) なら実在しないセンチネル1件 + ``spell_visible=False`` に倒し、可視面
    (システムプロンプトのスペル一覧) と実行ゲートの両方でどこにも出さない。

    ``or None`` で空を ``None`` にする従来パターンは ToolSchema のセマンティクス上
    「制限なし = 全 Building で visible」を意味してしまい、機体未登録の環境で身体
    ツールが全 Building へ露出する原因になっていた (この関数はその修正)。
    """
    if building_ids:
        return list(building_ids), True
    return [_NO_MATCHING_BUILDING], False


def effective_units(vessel: Any) -> List[Dict[str, Any]]:
    """vessel の有効なユニット配置リストを返す (docs/intent/stackchan_unit_placement.md §3)。

    要素: ``{"type": str, "channel": Optional[int], "label": str}``。
    ``unit_config.units`` があればそれを正規化して返す。無ければ legacy
    ``capabilities`` (True の type を channel=None / label="" で展開) にフォール
    バックする (additive・非破壊。新 UI で保存するまで従来挙動を保つ)。
    """
    cfg = getattr(vessel, "unit_config", None)
    if isinstance(cfg, dict) and isinstance(cfg.get("units"), list):
        out: List[Dict[str, Any]] = []
        for u in cfg["units"]:
            if not isinstance(u, dict) or not u.get("type"):
                continue
            ch = u.get("channel")
            out.append({
                "type": str(u["type"]),
                # bool は int のサブクラスなので明示除外 (True/False を channel に
                # しない)
                "channel": ch if isinstance(ch, int) and not isinstance(ch, bool)
                else None,
                "label": str(u.get("label") or ""),
            })
        return out
    caps = getattr(vessel, "capabilities", None) or {}
    return [
        {"type": str(t), "channel": None, "label": ""}
        for t, on in caps.items() if on
    ]


def units_of_type(vessel: Any, unit_type: str) -> List[Dict[str, Any]]:
    """vessel の配置のうち指定 type のユニットだけを返す。"""
    return [u for u in effective_units(vessel) if u["type"] == unit_type]


def list_building_ids_with_capability(cap_key: str) -> list:
    """指定 capability を持つ機体の Vessel Building の building_id リスト。

    ユニット由来ツール (env3 / servo8 / sonic / tof) の building_ids 用 (intent
    不変条件 #14 ユニット側)。 そのユニットを積んだ機体の building でだけ
    visible にする。 判定は per-vessel の配置 (:func:`effective_units`) に type が
    1 件以上含まれるか。 該当機体ゼロなら空リスト = どこにも出さない。
    """
    from vessel_manager import get_vessel_manager

    ids: list = []
    for v in get_vessel_manager().list_vessels():
        bid = v.bound_building_id
        if bid and bid not in ids and units_of_type(v, cap_key):
            ids.append(bid)
    return ids


def reregister_unit_tools() -> int:
    """全 native unit tool を ``schemas()`` 再評価で登録し直す。

    機体の capability (vessels.db) を変更した直後に呼ぶ。unit tool の
    ``spell_visible`` / ``building_ids`` は起動時のツール登録で一度だけ計算され、
    以後更新されない。起動後に初めて capability を ON にしたユニットは
    ``spell_visible=False`` のまま = ペルソナのスペル一覧に出ず、再起動するまで
    呼べない。ここで各 unit tool の ``schemas()`` を **現在の vessels.db** に対して
    再評価し、TOOL_REGISTRY / SPELL_TOOL_SCHEMAS / building ゲートを貼り直すことで
    再起動なしに反映する (docs/issues/stackchan_unit_capability_requires_restart.md
    バグ②)。

    対象の識別: ロード済みモジュールのうち ``MY_UNIT_CAP_KEY`` 属性と ``schemas()``
    を併せ持つもの (= ``tools/units/`` の native unit tool。README の規約)。
    move_head / see 等の共通身体ツールや複合アクション spell は
    ``MY_UNIT_CAP_KEY`` を持たないので対象外。

    Returns:
        再登録した tool の件数。
    """
    import sys

    from tools import _add_registered_tool, _remove_registered_tool

    count = 0
    module_count = 0
    for module in list(sys.modules.values()):
        if module is None:
            continue
        try:
            cap_marker = getattr(module, "MY_UNIT_CAP_KEY", None)
            schemas_fn = getattr(module, "schemas", None)
        except Exception:
            # PEP 562 module-level __getattr__ が例外を投げるような病的ケースは無視
            continue
        if not cap_marker or not callable(schemas_fn):
            continue
        module_count += 1
        try:
            fresh_schemas = schemas_fn()
        except Exception:
            LOGGER.exception(
                "reregister_unit_tools: schemas() failed for %s",
                getattr(module, "__name__", module),
            )
            continue
        for meta in fresh_schemas:
            impl = getattr(module, meta.name, None)
            if not callable(impl):
                LOGGER.warning(
                    "reregister_unit_tools: no impl for '%s' in %s",
                    meta.name, getattr(module, "__name__", module),
                )
                continue
            # 起動時の _register_multiple_tools と同様に addon_name を注入する。
            # schemas() / _build_schema は addon_name を設定しないので、ここで
            # 補わないと再登録後の schema が addon_name=None になり、
            # get_available_tool_schemas (複合アクションのツール一覧) 等の
            # addon_name フィルタから漏れて「--ツール--」表示になる。
            if not getattr(meta, "addon_name", None):
                meta.addon_name = getattr(module, "ADDON_NAME", None)
            # remove → add で gate クロージャ・可視面 (SPELL_TOOL_SCHEMAS) を貼り
            # 直す。impl は **生実装 (未ラップ)** を渡す: _add_registered_tool が
            # building_ids から gate を巻き直すので、TOOL_REGISTRY のラップ済み
            # func を渡すと二重ラップになる (module から直接引けば必ず生実装)。
            _remove_registered_tool(meta.name)
            _add_registered_tool(meta.name, meta, impl)
            count += 1

    if module_count == 0:
        LOGGER.warning(
            "reregister_unit_tools: no unit tool modules found "
            "(MY_UNIT_CAP_KEY marker missing); capability change will NOT reflect "
            "until restart"
        )
    else:
        LOGGER.info(
            "reregister_unit_tools: re-registered %d spell(s) from %d unit "
            "module(s)", count, module_count,
        )
    return count


__all__ = [
    "VesselNotAvailable",
    "MCP_QUALIFIED_SERVER",
    "resolve_vessel",
    "resolve_vessel_connection",
    "resolve_vessel_connection_for_building",
    "list_vessel_building_ids",
    "list_building_ids_with_capability",
    "building_gate_or_hidden",
    "effective_units",
    "units_of_type",
    "reregister_unit_tools",
]
