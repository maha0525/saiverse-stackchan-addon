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
from typing import Any, Optional, Tuple

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

    Raises:
        VesselNotAvailable: ペルソナ / Building が解決できない、 または
            その Building に紐付く機体が無い場合。
    """
    persona_id, building_id = _current_persona_and_building()
    if not persona_id or not building_id:
        raise VesselNotAvailable(
            "現在のペルソナまたは Building を解決できませんでした"
        )
    from vessel_manager import get_vessel_manager

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
    # 対象機体の明示上書き (addon 管理 UI の複合アクション「テスト実行」用)。
    # テスト実行は persona 文脈を持たないため、 通常の「現在ペルソナが降りて
    # いる機体」解決 (:func:`resolve_vessel`) が効かない。 UI で選んだ機体の
    # vessel_id が core の contextvar 経由で渡ってくるので、 それがあれば
    # persona 文脈より優先してその機体へ直接向ける。 通常のスペル経路では None
    # なので、 従来の persona 文脈解決に落ちる。
    from tools.context import get_tool_target_instance_id

    forced_vessel_id = get_tool_target_instance_id()
    if forced_vessel_id:
        from vessel_manager import get_vessel_manager

        vessel = get_vessel_manager().get_vessel(forced_vessel_id)
        if vessel is None:
            raise VesselNotAvailable(
                f"機体 '{forced_vessel_id}' が登録されていません "
                "(テスト対象の機体が削除された可能性)"
            )
    else:
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


def list_building_ids_with_capability(cap_key: str) -> list:
    """指定 capability を持つ機体の Vessel Building の building_id リスト。

    ユニット由来ツール (env3 / servo8 / sonic) の building_ids 用 (intent
    不変条件 #14 ユニット側)。 そのユニットを積んだ機体の building でだけ
    visible にする。 capabilities は vessels.db の per-vessel JSON (機体管理 UI
    で手動設定、 Phase 8' で自動検出)。 該当機体ゼロなら空リスト = どこにも
    出さない。
    """
    from vessel_manager import get_vessel_manager

    ids: list = []
    for v in get_vessel_manager().list_vessels():
        caps = v.capabilities or {}
        bid = v.bound_building_id
        if caps.get(cap_key) and bid and bid not in ids:
            ids.append(bid)
    return ids


__all__ = [
    "VesselNotAvailable",
    "MCP_QUALIFIED_SERVER",
    "resolve_vessel",
    "resolve_vessel_connection",
    "resolve_vessel_connection_for_building",
    "list_vessel_building_ids",
    "list_building_ids_with_capability",
]
