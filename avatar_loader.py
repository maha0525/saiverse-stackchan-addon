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
# 注意: device 側 firmware は ``self.display.set_avatar`` で AddTool して
# いるが、 gateway は SAIVerse の MCP client に対しては bare 名 ``set_avatar``
# で再 expose している。 ここで呼ぶのは gateway の expose 名 = 短い方。
MCP_TOOL_SET_AVATAR = "set_avatar"
# device の boot session id を含む device status を取る。 SAIVerse 側
# avatar cache (_last_loaded) と device 実状態の不整合を解消するため、
# 入室時に session_id を確認して reboot を検知する目的で使う。
MCP_TOOL_GET_DEVICE_STATUS = "get_device_status"

# load_avatar_set 全体のタイムアウト (HTTP 転送 + ESP32 PSRAM 書き込みを
# 含む)。 MCP tool 側のデフォルトは 60 s、 こちらは余裕を見て 90 s。
_LOAD_TIMEOUT_SEC = 90.0
# set_avatar(idle/off) は 1 回の WS frame 往復だけなので短めで OK。
_SET_AVATAR_TIMEOUT_SEC = 10.0
# get_device_status は 1 回の WS frame 往復だけ、 入室経路に乗せるので
# レイテンシを抑えたい。
_GET_STATUS_TIMEOUT_SEC = 5.0


class AvatarSetLoader:
    """憑依時の avatar セット自動ロードを管理する singleton。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # device の PSRAM に現在 adopt されてる avatar set の checksum 1 つ。
        # device は avatar set を同時に 1 つしか保持できず、 新規 load で
        # 旧 set は上書きされて消える (= adopt 経路、 firmware
        # AvatarSetFetcher::AdoptOwnedBuffer 参照)。 SAIVerse 側で
        # ペルソナ別に「load 済み」を覚える設計は実機モデルと合わないため、
        # ここでは device 状態を 1:1 で trace する。
        # ``None`` = device の現状態不明 (起動直後、 session reset 後)。
        self._device_current_checksum: Optional[str] = None
        # 現在 Vessel Building にいるペルソナ (= まはー検証 2026-05-17、
        # ⑤ finalize 時に自動転送するか判定するため、 in-memory で記録)。
        # 入室 / 退室 hook で更新、 SAIVerse 再起動でリセット。
        self._currently_vessel_persona: Optional[str] = None
        # device の最後に観測した boot_session_id。 device 側で boot ごとに
        # esp_random で生成される UUID。 入室時に都度 get_device_status で
        # 取得し、 前回と違ったら device が reboot した = PSRAM クリア =
        # cache を invalidate する必要がある、と判定する。
        # 古い firmware (boot_session_id を返さない) には対応せず保守的に
        # cache 維持する (= None のまま動作、 invalidate しない)。
        self._last_seen_session_id: Optional[str] = None

    def mark_persona_entered(self, persona_id: str) -> None:
        with self._lock:
            self._currently_vessel_persona = persona_id

    def mark_persona_exited(self, persona_id: str) -> None:
        with self._lock:
            if self._currently_vessel_persona == persona_id:
                self._currently_vessel_persona = None

    def is_persona_in_vessel(self, persona_id: str) -> bool:
        with self._lock:
            return self._currently_vessel_persona == persona_id

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
        """device に現在 adopt されてる avatar checksum と比較し、 再ロード
        が必要なら True。 ``persona_id`` は API 互換用 (= 内部判定は
        checksum のみで完結、 device は 1 set しか持たないため)。

        ``checksum`` が空文字列なら常に再ロード扱い (= manifest に
        checksum がない / 古いフォーマット)。
        """
        del persona_id  # device は 1 avatar set のみ保持、 persona 別比較不要
        if not checksum:
            return True
        with self._lock:
            return self._device_current_checksum != checksum

    def mark_loaded(self, persona_id: str, checksum: str) -> None:
        """device に avatar が adopt されたことを記録する。 device は
        1 set しか保持しないので、 ``persona_id`` は使わず checksum を
        上書きする (= 新 set adopt で旧は捨てられる挙動と一致)。
        """
        del persona_id
        if not checksum:
            return
        with self._lock:
            self._device_current_checksum = checksum

    def clear_cache(self, persona_id: Optional[str] = None) -> None:
        """テスト / 手動再ロード用。 ``persona_id`` 引数は API 互換のみで
        実際の挙動には影響しない (device は 1 set 保持なので全消去のみ
        意味がある)。
        """
        del persona_id
        with self._lock:
            self._device_current_checksum = None

    def reconcile_session(self, current_session_id: Optional[str]) -> None:
        """device の boot_session_id を比較して、 reboot を検知した場合に
        cache を invalidate する。

        ``current_session_id`` が ``None`` の場合 (= device 不在、 古い
        firmware で session_id を返さない、 通信エラー等) は保守的に何も
        しない (= cache 維持)。 これにより古い firmware との互換も保つ。
        """
        if current_session_id is None:
            return
        with self._lock:
            previous = self._last_seen_session_id
            if previous is None:
                # 初回観測 — session_id を記録するのみ。 cache は既に
                # 空のはず (= SAIVerse 起動直後の状態) なので invalidate
                # 不要だが、 念のため None にしておく。
                self._last_seen_session_id = current_session_id
                self._device_current_checksum = None
                return
            if previous != current_session_id:
                LOGGER.info(
                    "avatar_loader: device boot session changed (%s -> %s),"
                    " clearing cached avatar checksum (was %s)",
                    previous, current_session_id,
                    self._device_current_checksum,
                )
                self._device_current_checksum = None
                self._last_seen_session_id = current_session_id


_loader = AvatarSetLoader()


def get_avatar_loader() -> AvatarSetLoader:
    return _loader


# ----- Hook handler -----


def _vessel_building_id() -> Optional[str]:
    """AddonConfig から Vessel Building ID を取得する。"""
    params = get_params(ADDON_NAME, persona_id=None) or {}
    vbid = params.get("vessel_building_id")
    return vbid if isinstance(vbid, str) and vbid else None


def _get_active_set_name(persona_id: str) -> str:
    """avatar_pipeline からアクティブセット名を取得 (Phase 4.5-d-5)。

    `<storage>/avatar_sets/<persona_id>/_active.json` を読む。 ファイルが
    無い / 読めない / pipeline モジュール未配置の場合は DEFAULT_SET_NAME
    にフォールバック (= 既存挙動と一致、 後方互換)。

    lazy import で書く理由: avatar_loader は server_hook (= addon ロード時に
    register される) なので、 import 時点で avatar_pipeline まで強制 import
    したくない (= module ロード順序の依存を作らない)。
    """
    try:
        from avatar_pipeline import get_avatar_pipeline_manager
        active = get_avatar_pipeline_manager().get_active(persona_id)
        if active:
            return active
    except Exception as exc:
        LOGGER.warning(
            "avatar_loader: failed to resolve active set name "
            "for persona=%s, falling back to %r: %s",
            persona_id, DEFAULT_SET_NAME, exc,
        )
    return DEFAULT_SET_NAME


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
    """gateway の ``set_avatar`` MCP tool を呼ぶ (gateway 側 expose 名)。

    face: ``idle``/``happy``/``thinking``/``sad``/``surprised``/
    ``embarrassed`` のいずれか、 もしくは ``off`` (= レイヤを隠す)。
    """
    conn = await _get_stackchan_conn()
    return await conn.call_tool(MCP_TOOL_SET_AVATAR, {"face": face})


async def _call_get_device_status() -> str:
    """gateway の ``get_device_status`` MCP tool を呼ぶ。

    新 firmware は JSON に ``boot_session_id`` フィールドを含める。 これを
    SAIVerse 側で記録 / 比較して device reboot を検知する。
    """
    conn = await _get_stackchan_conn()
    return await conn.call_tool(MCP_TOOL_GET_DEVICE_STATUS, {})


def _fetch_device_session_id() -> Optional[str]:
    """device の現在の ``boot_session_id`` を取得する。 取れなかったら ``None``。

    保守的な失敗扱い: タイムアウト / MCP エラー / 古い firmware (= フィールド
    なし) / JSON parse 失敗 のいずれも ``None`` 扱い。 呼び出し元は ``None``
    を受け取った場合は cache を invalidate しない (= 古い firmware との
    互換維持)。
    """
    result = _run_async_on_mcp_loop(
        _call_get_device_status(), timeout_sec=_GET_STATUS_TIMEOUT_SEC,
    )
    if not result:
        return None
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    sid = parsed.get("boot_session_id")
    if not isinstance(sid, str) or not sid:
        return None
    return sid


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

    # in-vessel 記録 (= ⑤ finalize 時の自動転送判定用、 まはー検証 2026-05-17)。
    loader = get_avatar_loader()
    loader.mark_persona_entered(persona_id)

    # device の boot_session_id を取得して、 前回観測と比較する。 異なれば
    # device が reboot した = PSRAM がクリアされた = `_last_loaded` cache
    # は無効 (= "load 済み" と思ってる avatar は実際には device に存在
    # しない) なので、 cache を全クリアして強制再 transfer に倒す。
    # `None` (= 古い firmware で boot_session_id を返さない / 通信エラー)
    # は保守的に無視 (= 既存挙動を維持)。
    current_session_id = _fetch_device_session_id()
    loader.reconcile_session(current_session_id)

    # 1. avatar セットを load (= 配置されてれば)。
    # アクティブセット名を avatar_pipeline から引く (Phase 4.5-d-5)。
    # 4.5-d-5 以前は DEFAULT_SET_NAME 固定だったが、 複数バリエーション
    # 対応のために `<persona_id>/_active.json` を見るようにする。
    active_set_name = _get_active_set_name(persona_id)
    found = loader.find_persona_set(persona_id, active_set_name)
    if found is None:
        LOGGER.info(
            "avatar_loader: no avatar set on disk for persona=%s "
            "(expected at %s) — skip auto-load, will still reset face state",
            persona_id, loader.set_dir(persona_id, active_set_name),
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
                # gateway は load_avatar_set の結果を JSON 文字列で返す
                # ({"ok": bool, "checksum": str, "error": str|null,
                # "bytes_transferred": int})。 device 不在等で転送失敗
                # ("ok": false) なのに mark_loaded すると、 次回入室で
                # 「load 済み」と誤認して skip 経路に入り、 そのペルソナ
                # の avatar が永久に device に届かなくなる。 ok=true の
                # 時だけマークし、 失敗時は WARNING を残して次回再試行
                # を許容する。
                try:
                    parsed = (
                        json.loads(result)
                        if isinstance(result, str)
                        else result
                    )
                except (json.JSONDecodeError, TypeError):
                    parsed = None
                if isinstance(parsed, dict) and parsed.get("ok") is True:
                    loader.mark_loaded(persona_id, checksum)
                else:
                    LOGGER.warning(
                        "avatar_loader: load FAILED for persona=%s "
                        "mode=%s result=%s — not marking as loaded, "
                        "will retry on next vessel entry",
                        persona_id, mode, result,
                    )

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
    # in-vessel 記録から削除 (= ⑤ finalize の auto-transfer 対象外に)。
    get_avatar_loader().mark_persona_exited(persona_id)
    _run_async_on_mcp_loop(
        _call_set_avatar("off"), timeout_sec=_SET_AVATAR_TIMEOUT_SEC,
    )
