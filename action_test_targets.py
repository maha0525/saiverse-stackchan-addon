"""複合アクションの「テスト実行先」候補 (機体) を本体 UI に供給する provider。

addon 管理 UI の複合アクションセクション (本体 ``ActionsPanel``) から呼ばれる。
複数機体では native tool (move_head / see / ...) が「今ペルソナが降りている機体」
へ振り分けるため、 persona 文脈を持たないテスト実行では対象機体をユーザーに選ば
せる必要がある。 本 provider は登録済み vessel を ``{value: vessel_id, label:
表示名}`` の一覧にして返し、 本体はそれを機体プルダウンにする (intent
stackchan_vessel.md 設計 K-7、 デバイス操作 UI の機体セレクタと同じ考え方)。

宣言: ``addon.json`` の ``action_test_targets: "action_test_targets:
list_action_test_targets"``。 本体 ``saiverse.addon_loader.
get_addon_action_test_targets`` が server_hooks と同じ ``module:function`` 解決で
呼ぶ。

import 解決: 本体の module loader は addon の ``tools/`` までしか sys.path に積ま
ないうえ、 本モジュールは addon root 直下なので、 ここで addon root を sys.path
に積んで ``vessel_manager`` を import できるようにする (move_head.py 等と同じ
事情)。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Dict, List

_ADDON_ROOT = str(Path(__file__).resolve().parent)
if _ADDON_ROOT not in sys.path:
    sys.path.insert(0, _ADDON_ROOT)

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"
MCP_QUALIFIED_SERVER = f"{ADDON_NAME}__stackchan"


def _connected_vessel_ids() -> set:
    """gateway インスタンスが実際に接続済みの vessel_id 集合。

    テスト実行が成功するのは「その機体の gateway が接続されている」ときだけ
    なので、 ラベルに接続状態を添えてユーザーが選び間違えないようにする。
    MCP manager が真実の source (``{server}:instance:{vessel_id}`` 接続の有無)。
    """
    try:
        from tools.mcp_client import _make_instance_key, get_mcp_manager

        mcp = get_mcp_manager()
        if mcp is None:
            return set()
        connected = set()
        for vid in _all_vessel_ids():
            key = _make_instance_key(MCP_QUALIFIED_SERVER, instance_id=vid)
            if mcp._connections.get(key) is not None:
                connected.add(vid)
        return connected
    except Exception:
        LOGGER.debug("action_test_targets: connection probe failed", exc_info=True)
        return set()


def _all_vessel_ids() -> List[str]:
    from vessel_manager import get_vessel_manager

    return [v.vessel_id for v in get_vessel_manager().list_vessels()]


def list_action_test_targets() -> List[Dict[str, str]]:
    """登録済み機体を本体 UI 用の ``{value, label}`` 一覧にして返す。

    ``value`` は vessel_id (= MCP インスタンス id、 本体はこれをテスト実行 API の
    ``instance_id`` として渡す)。 ``label`` は「何も知らない人が分かる」表示名と
    して、 デバイス操作 UI の機体セレクタと同じ ``<building> (<vessel短縮> /
    <接続状態>)`` 形式にする。 機体未登録なら空リスト (= UI はプルダウンを出さ
    ない)。
    """
    from vessel_manager import get_vessel_manager

    vessels = get_vessel_manager().list_vessels()
    if not vessels:
        return []

    connected = _connected_vessel_ids()
    targets: List[Dict[str, str]] = []
    for v in vessels:
        short = v.vessel_id[:8]
        status = "接続中" if v.vessel_id in connected else "未接続"
        targets.append({
            "value": v.vessel_id,
            "label": f"{v.bound_building_id} ({short}… / {status})",
        })
    return targets
