"""Stack-chan Vessel: avatar セット永続化 + 憑依時自動ロード (Phase 4.5-c)。

ペルソナごとの avatar セット (= raw RGB565 .bin + manifest.json) を addon
storage に保持し、 ペルソナが Vessel Building に入室したタイミングで gateway
経由で ESP32 に転送する。 ロード自体は本体 MCP client が起動済みの
``stackchan-mcp`` gateway の ``load_avatar_set`` ツールを叩いて行う。

ストレージレイアウト:

    ~/.saiverse/addons/saiverse-stackchan-addon/
        avatar_sets/
            <persona_id>/
                default/             # 最初のリリースでは set 名は固定 "default"
                    avatar.bin       # raw RGB565 payload (537,600 or 3,456,000 bytes)
                    manifest.json    # {"mode": "layered"|"matrix", "checksum": "sha256:..."}

連続ロードのスキップ:
    同ペルソナを連続憑依させたり、 短時間に再入室イベントが来たりした時に
    同じセットを都度再転送するのは無駄。 in-memory に
    ``{persona_id: last_checksum}`` を持ち一致したらスキップする。 SAIVerse
    再起動時にはリセットされる (= 再起動直後は 1 回必ずロードされる)。

詳細: docs/intent/stackchan_avatar_pipeline.md §D-1, §D-2 (Phase 4.5-c)
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from pathlib import Path
from typing import Optional

# addon_loader の spec_from_file_location 経由ロードでは __package__ が
# 設定されないため相対 import が動かない。 同梱モジュールを絶対 import
# するためにパック自身のディレクトリを sys.path に追加する。
_PACK_DIR = str(Path(__file__).parent)
if _PACK_DIR not in sys.path:
    sys.path.insert(0, _PACK_DIR)

from saiverse.addon_config import get_params  # noqa: E402
from saiverse.addon_paths import get_addon_storage_path  # noqa: E402

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"
DEFAULT_SET_NAME = "default"
MCP_QUALIFIED_SERVER = f"{ADDON_NAME}__stackchan"
MCP_TOOL_NAME = "load_avatar_set"

# load_avatar_set 全体のタイムアウト (HTTP 転送 + ESP32 PSRAM 書き込みを
# 含む)。 MCP tool 側のデフォルトは 60 s、 こちらは余裕を見て 90 s。
_LOAD_TIMEOUT_SEC = 90.0


class AvatarSetLoader:
    """憑依時の avatar セット自動ロードを管理する singleton。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # persona_id -> last loaded checksum string ("sha256:...")
        self._last_loaded: dict[str, str] = {}

    # ----- Storage layout -----

    def storage_root(self) -> Path:
        return get_addon_storage_path(ADDON_NAME) / "avatar_sets"

    def set_dir(
        self, persona_id: str, set_name: str = DEFAULT_SET_NAME
    ) -> Path:
        return self.storage_root() / persona_id / set_name

    def find_persona_set(
        self, persona_id: str, set_name: str = DEFAULT_SET_NAME
    ) -> Optional[tuple[Path, dict]]:
        """ペルソナの avatar セット (= ``avatar.bin`` + ``manifest.json``)
        を返す。 セット未配置 / manifest 不正なら ``None``。
        """
        set_dir = self.set_dir(persona_id, set_name)
        bin_path = set_dir / "avatar.bin"
        manifest_path = set_dir / "manifest.json"
        if not bin_path.exists() or not manifest_path.exists():
            return None
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:
            LOGGER.warning(
                "avatar_loader: failed to parse manifest %s: %s",
                manifest_path, exc,
            )
            return None
        if not isinstance(manifest, dict):
            LOGGER.warning(
                "avatar_loader: manifest %s is not an object", manifest_path,
            )
            return None
        return bin_path, manifest

    # ----- Cache (連続ロードのスキップ判定) -----

    def is_load_required(self, persona_id: str, checksum: str) -> bool:
        """前回ロード済みの checksum と比較し、 再ロードが必要なら True。

        ``checksum`` が空文字列なら常に再ロード扱い (= manifest に
        checksum がない / 古いフォーマット)。
        """
        if not checksum:
            return True
        with self._lock:
            return self._last_loaded.get(persona_id) != checksum

    def mark_loaded(self, persona_id: str, checksum: str) -> None:
        if not checksum:
            return
        with self._lock:
            self._last_loaded[persona_id] = checksum

    def clear_cache(self, persona_id: Optional[str] = None) -> None:
        """テスト / 手動再ロード用。 ``persona_id=None`` で全消去。"""
        with self._lock:
            if persona_id is None:
                self._last_loaded.clear()
            else:
                self._last_loaded.pop(persona_id, None)


_loader = AvatarSetLoader()


def get_avatar_loader() -> AvatarSetLoader:
    return _loader


# ----- Hook handler -----


def _vessel_building_id() -> Optional[str]:
    """AddonConfig から Vessel Building ID を取得する。"""
    params = get_params(ADDON_NAME, persona_id=None) or {}
    vbid = params.get("vessel_building_id")
    return vbid if isinstance(vbid, str) and vbid else None


async def _call_load_avatar_set(archive_path: str, mode: str) -> str:
    """gateway の ``load_avatar_set`` MCP tool を呼ぶ (MCP loop 上で実行)。

    本体 MCP client が起動済みの ``saiverse-stackchan-addon__stackchan``
    インスタンス (= global scope) に直接 ``call_tool`` する。 接続未確立
    なら ``RuntimeError``。
    """
    from tools.mcp_client import get_mcp_manager, _make_instance_key

    manager = get_mcp_manager()
    if manager is None:
        raise RuntimeError("MCP manager is not initialized")
    instance_key = _make_instance_key(MCP_QUALIFIED_SERVER, persona_id=None)
    conn = manager._connections.get(instance_key)
    if conn is None:
        raise RuntimeError(
            f"MCP server '{MCP_QUALIFIED_SERVER}' is not connected; "
            "avatar load skipped"
        )
    return await conn.call_tool(
        MCP_TOOL_NAME,
        {"archive_path": archive_path, "mode": mode},
    )


def on_persona_entered_building(
    persona_id: str,
    building_id: str,
    from_building_id: Optional[str] = None,
    **_kwargs,
) -> None:
    """``persona_entered_building`` server_hook ハンドラ。

    addon_hooks の ThreadPoolExecutor から呼ばれる (= 別スレッド)。
    Vessel Building 以外への入室は早期 return、 該当時のみ avatar セット
    を解決して gateway に転送する。

    エラー (= avatar 未配置 / MCP 未接続 / load_avatar_set 失敗) は
    すべて WARNING ログに留めて飲み込む。 ペルソナ移動自体は成功して
    いる以上、 avatar の都合で例外を伝播させて移動経路を壊さない。
    """
    vessel_bid = _vessel_building_id()
    if not vessel_bid:
        # Vessel Building ID が未設定 → addon の物理機能全体が無効状態。
        return
    if building_id != vessel_bid:
        # Vessel Building 以外への入室は対象外。
        return

    loader = get_avatar_loader()
    found = loader.find_persona_set(persona_id)
    if found is None:
        LOGGER.info(
            "avatar_loader: no avatar set on disk for persona=%s "
            "(expected at %s) — skip auto-load",
            persona_id, loader.set_dir(persona_id),
        )
        return
    bin_path, manifest = found
    mode = manifest.get("mode")
    checksum = manifest.get("checksum") or ""
    if mode not in ("layered", "matrix"):
        LOGGER.warning(
            "avatar_loader: invalid manifest mode for persona=%s: %r "
            "(allowed: layered / matrix)",
            persona_id, mode,
        )
        return
    if not loader.is_load_required(persona_id, checksum):
        LOGGER.info(
            "avatar_loader: persona=%s already loaded (checksum=%s), skip",
            persona_id, checksum,
        )
        return

    # MCP loop に coro を投げて結果を待つ。 hook handler が走ってる
    # ThreadPoolExecutor の worker thread は MCP loop と別なので
    # asyncio.run_coroutine_threadsafe で bridge する。
    # ``_loop`` は mcp_client の module-level binding (実行時に値が
    # 変わるので、 import 時の binding ではなく属性アクセスで読む)。
    import tools.mcp_client as _mcp

    loop = _mcp._loop
    if loop is None:
        LOGGER.warning(
            "avatar_loader: MCP event loop not initialized, cannot load "
            "avatar for persona=%s",
            persona_id,
        )
        return

    LOGGER.info(
        "avatar_loader: loading avatar for persona=%s mode=%s "
        "(bin=%s, %d bytes)",
        persona_id, mode, bin_path, bin_path.stat().st_size,
    )
    future = asyncio.run_coroutine_threadsafe(
        _call_load_avatar_set(str(bin_path), mode), loop,
    )
    try:
        result = future.result(timeout=_LOAD_TIMEOUT_SEC)
    except Exception as exc:
        LOGGER.warning(
            "avatar_loader: load_avatar_set failed for persona=%s: %s",
            persona_id, exc,
        )
        return

    LOGGER.info(
        "avatar_loader: loaded avatar for persona=%s mode=%s result=%s",
        persona_id, mode, result,
    )
    loader.mark_loaded(persona_id, checksum)
