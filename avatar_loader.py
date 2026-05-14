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
MCP_TOOL_LOAD_SET = "load_avatar_set"
MCP_TOOL_SET_AVATAR = "self.display.set_avatar"

# load_avatar_set 全体のタイムアウト (HTTP 転送 + ESP32 PSRAM 書き込みを
# 含む)。 MCP tool 側のデフォルトは 60 s、 こちらは余裕を見て 90 s。
_LOAD_TIMEOUT_SEC = 90.0
# set_avatar(idle/off) は 1 回の WS frame 往復だけなので短めで OK。
_SET_AVATAR_TIMEOUT_SEC = 10.0


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


async def _get_stackchan_conn():
    """本体 MCP client から stackchan gateway 接続を引く。"""
    from tools.mcp_client import get_mcp_manager, _make_instance_key

    manager = get_mcp_manager()
    if manager is None:
        raise RuntimeError("MCP manager is not initialized")
    instance_key = _make_instance_key(MCP_QUALIFIED_SERVER, persona_id=None)
    conn = manager._connections.get(instance_key)
    if conn is None:
        raise RuntimeError(
            f"MCP server '{MCP_QUALIFIED_SERVER}' is not connected"
        )
    return conn


async def _call_load_avatar_set(archive_path: str, mode: str) -> str:
    """gateway の ``load_avatar_set`` MCP tool を呼ぶ (MCP loop 上で実行)。"""
    conn = await _get_stackchan_conn()
    return await conn.call_tool(
        MCP_TOOL_LOAD_SET,
        {"archive_path": archive_path, "mode": mode},
    )


async def _call_set_avatar(face: str) -> str:
    """gateway の ``self.display.set_avatar`` MCP tool を呼ぶ。

    face: ``idle``/``happy``/``thinking``/``sad``/``surprised``/
    ``embarrassed`` のいずれか、 もしくは ``off`` (= レイヤを隠す)。
    """
    conn = await _get_stackchan_conn()
    return await conn.call_tool(MCP_TOOL_SET_AVATAR, {"face": face})


def _run_async_on_mcp_loop(coro, timeout_sec: float) -> Optional[str]:
    """sync 文脈から MCP loop の coro を呼んで結果を取る共通ヘルパ。

    呼び出し元 (= addon_hooks の ThreadPoolExecutor worker) は ``_loop``
    と別スレッドなので ``asyncio.run_coroutine_threadsafe`` で bridge する。
    失敗時は WARNING を残して ``None`` を返す (= 呼び出し元は続行可能)。
    """
    import tools.mcp_client as _mcp

    loop = _mcp._loop
    if loop is None:
        LOGGER.warning(
            "avatar_loader: MCP event loop not initialized, MCP call skipped"
        )
        return None
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout_sec)
    except Exception as exc:
        LOGGER.warning("avatar_loader: MCP call failed: %s", exc)
        return None


def on_persona_entered_building(
    persona_id: str,
    building_id: str,
    from_building_id: Optional[str] = None,
    **_kwargs,
) -> None:
    """``persona_entered_building`` server_hook ハンドラ。

    addon_hooks の ThreadPoolExecutor から呼ばれる (= 別スレッド)。
    Vessel Building 以外への入室は早期 return、 該当時は

      1. 該当ペルソナの avatar セットを (必要なら) gateway 経由で load
      2. デバイスの face/eyes/mouth 状態を ``idle`` にリセット (= 前
         ペルソナの ``happy`` 等が次ペルソナに引き継がれるのを防ぐ)

    の 2 ステップを順に走らせる。 (2) はセットの有無に関わらず常に
    実行する (= 状態リセットはセットを持っていないペルソナにも必要)。
    エラーは全部 WARNING にして飲み込む — ペルソナ移動自体は成功して
    いる以上、 avatar の都合で例外を伝播させて移動経路を壊さない。
    """
    vessel_bid = _vessel_building_id()
    if not vessel_bid:
        return
    if building_id != vessel_bid:
        return

    # 1. avatar セットを load (= 配置されてれば)。
    loader = get_avatar_loader()
    found = loader.find_persona_set(persona_id)
    if found is None:
        LOGGER.info(
            "avatar_loader: no avatar set on disk for persona=%s "
            "(expected at %s) — skip auto-load, will still reset face state",
            persona_id, loader.set_dir(persona_id),
        )
    else:
        bin_path, manifest = found
        mode = manifest.get("mode")
        checksum = manifest.get("checksum") or ""
        if mode not in ("layered", "matrix"):
            LOGGER.warning(
                "avatar_loader: invalid manifest mode for persona=%s: %r "
                "(allowed: layered / matrix)",
                persona_id, mode,
            )
        elif not loader.is_load_required(persona_id, checksum):
            LOGGER.info(
                "avatar_loader: persona=%s already loaded (checksum=%s), "
                "skip transfer",
                persona_id, checksum,
            )
        else:
            LOGGER.info(
                "avatar_loader: loading avatar for persona=%s mode=%s "
                "(bin=%s, %d bytes)",
                persona_id, mode, bin_path, bin_path.stat().st_size,
            )
            result = _run_async_on_mcp_loop(
                _call_load_avatar_set(str(bin_path), mode),
                timeout_sec=_LOAD_TIMEOUT_SEC,
            )
            if result is not None:
                LOGGER.info(
                    "avatar_loader: loaded avatar for persona=%s mode=%s "
                    "result=%s",
                    persona_id, mode, result,
                )
                loader.mark_loaded(persona_id, checksum)

    # 2. 状態リセット: 前ペルソナの face/mouth/blink 状態が device 側に
    # 残ってる可能性があるので、 idle を明示的に打って初期化する。
    # device 側の SetAvatarExpression は "off" → "idle" 遷移時にレイヤを
    # 自動 unhide + 必要なら blink を復元するので、 退室時に "off" を
    # 打った直後の入室でも正しく顔が出る。
    LOGGER.info(
        "avatar_loader: resetting device face to idle for persona=%s",
        persona_id,
    )
    _run_async_on_mcp_loop(
        _call_set_avatar("idle"), timeout_sec=_SET_AVATAR_TIMEOUT_SEC,
    )


def on_persona_exited_building(
    persona_id: str,
    building_id: str,
    from_building_id: Optional[str] = None,
    to_building_id: Optional[str] = None,
    **_kwargs,
) -> None:
    """``persona_exited_building`` server_hook ハンドラ。

    Vessel Building からの退室時に device の avatar レイヤを ``off`` で
    隠す (= 誰も憑依していない時間は顔を表示し続けない)。 Vessel が
    capacity=1 設計なので「退室 = vessel が空になる」が成立する前提。

    ``set_avatar("off")`` は firmware 側で blink 状態を保存して停止し、
    avatar lv_obj を ``LV_OBJ_FLAG_HIDDEN`` で隠すので、 LCD 自体は
    xiaozhi-esp32 の下層 UI (WiFi 設定や OTA 画面など) が見える状態に
    戻る。
    """
    vessel_bid = _vessel_building_id()
    if not vessel_bid:
        return
    # building_id (= 退室元) と vessel ID を照合。 dispatcher 側で
    # from_building_id にも同じ値を入れているがどちらでも OK。
    if building_id != vessel_bid:
        return

    LOGGER.info(
        "avatar_loader: vessel exit by persona=%s — hiding avatar layer",
        persona_id,
    )
    _run_async_on_mcp_loop(
        _call_set_avatar("off"), timeout_sec=_SET_AVATAR_TIMEOUT_SEC,
    )
