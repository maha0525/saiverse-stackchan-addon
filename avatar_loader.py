"""Stack-chan Vessel: avatar セット永続化 + 憑依時自動ロード (Phase 4.5-c)。

ペルソナごとの avatar セット (= raw RGB565 .bin + manifest.json) を addon
storage に保持し、 ペルソナが Vessel Building に入室したタイミングで gateway
経由で ESP32 に転送する。 ロード自体は本体 MCP client が起動済みの
``stackchan-mcp`` gateway の ``load_avatar_set`` ツールを叩いて行う。

ストレージレイアウト:

    ~/.saiverse/user_data/addon_data/saiverse-stackchan-addon/
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
import time
from pathlib import Path
from typing import Callable, Optional

# addon_loader の spec_from_file_location 経由ロードでは __package__ が
# 設定されないため相対 import が動かない。 同梱モジュールを絶対 import
# するためにパック自身のディレクトリを sys.path に追加する。
_PACK_DIR = str(Path(__file__).parent)
if _PACK_DIR not in sys.path:
    sys.path.insert(0, _PACK_DIR)

from saiverse.addon_config import get_params  # noqa: E402
from saiverse.addon_paths import get_addon_data_dir  # noqa: E402

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
# avatar cache (_device_current_checksum) と device 実状態の不整合を解消
# するため、 入室時に session_id を確認して reboot を検知する目的で使う。
# gateway の bare 名は ``get_device_info``、 これが内部で ESP32 の
# ``self.get_device_status`` に relay される (stdio_server.py:735 の
# tool_map 参照、 set_avatar が ``self.display.set_avatar`` に 対応する
# のと同じパターン)。
MCP_TOOL_GET_DEVICE_STATUS = "get_device_info"

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
        return get_addon_data_dir(ADDON_NAME) / "avatar_sets"

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

    def reconcile_session(
        self,
        current_session_id: Optional[str],
        on_invalidate: Optional[Callable[[], None]] = None,
    ) -> None:
        """device の boot_session_id を比較して、 reboot を検知した場合に
        cache を invalidate する。 ``on_invalidate`` が指定されていれば、
        実際に invalidate が発生した時に lock 解放後に呼び出す (= polling
        loop が再 transfer を発火する用途、 callback 内で再帰的に lock
        を取らないため)。

        ``current_session_id`` が ``None`` の場合 (= device 不在、 古い
        firmware で session_id を返さない、 通信エラー等) は保守的に何も
        しない (= cache 維持)。 これにより古い firmware との互換も保つ。
        """
        if current_session_id is None:
            return
        invalidated = False
        with self._lock:
            previous = self._last_seen_session_id
            if previous is None:
                # 初回観測 — session_id を記録するのみ。 cache は触らない:
                # 起動時 reconcile が既に成功してれば正しい checksum が
                # 入っているのでそれを尊重、 失敗してれば空のままで次回
                # 入室時に lazy load される (= 既存挙動と一致)。
                self._last_seen_session_id = current_session_id
            elif previous != current_session_id:
                LOGGER.info(
                    "avatar_loader: device boot session changed (%s -> %s),"
                    " clearing cached avatar checksum (was %s)",
                    previous, current_session_id,
                    self._device_current_checksum,
                )
                self._device_current_checksum = None
                self._last_seen_session_id = current_session_id
                invalidated = True
        if invalidated and on_invalidate is not None:
            try:
                on_invalidate()
            except Exception:
                LOGGER.exception(
                    "avatar_loader: on_invalidate callback failed"
                )


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
        LOGGER.debug("_fetch_device_session_id: result is empty/None")
        return None
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except (json.JSONDecodeError, TypeError) as exc:
        LOGGER.warning(
            "_fetch_device_session_id: JSON parse failed: %s, raw=%r",
            exc, str(result)[:300],
        )
        return None
    if not isinstance(parsed, dict):
        LOGGER.warning(
            "_fetch_device_session_id: parsed not dict, type=%s raw=%r",
            type(parsed).__name__, str(result)[:300],
        )
        return None
    sid = parsed.get("boot_session_id")
    if not isinstance(sid, str) or not sid:
        LOGGER.warning(
            "_fetch_device_session_id: no boot_session_id field "
            "(keys=%s, raw=%r)",
            list(parsed.keys()), str(result)[:300],
        )
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


# ----- Background reconcile (state 同期) -----
#
# 入室時の lazy check (= on_persona_entered_building) だけだと、 以下 2 つの
# シナリオで device 状態と SAIVerse cache が乖離する:
#
#   A. SAIVerse 起動中に device 単独で reboot した場合 — 退室 / 再入室
#      が無ければ気付かない
#   B. device が blank の状態で、 既に vessel に居る persona が登録された
#      まま SAIVerse を起動した場合 — そもそも入室 event が発火しないので
#      avatar transfer が走らない
#
# 設計:
#   - シナリオ B (起動時の同期) は本体 MCP client の ``on_server_ready``
#     hook で event driven に発火。 gateway subprocess の接続完了を待って
#     から initial reconcile を 1 回実行する。 timed polling (旧設計の
#     8 秒固定 + 30 秒 tick) より大幅に応答性が良い。
#   - シナリオ A (起動後の device 単独 reboot) は session_id の polling で
#     検知。 gateway は device WS 切断/再接続を SAIVerse に通知しない
#     構造的制約があるため、 device 状態を観測する経路は polling が残る。
#
# どちらも daemon thread で動かす (= SAIVerse プロセス停止で自動終了)。

# 通常の session_id polling 間隔 (= initial reconcile 完了後の device 単独
# reboot 検知用)。 reboot 検知の応答性 vs MCP round trip コストのバランス。
_POLL_INTERVAL_SEC = 30.0
# MCP server ready 後の **ESP32 接続待ち burst window**。 重要な分離:
# - MCP server ready = gateway subprocess (= host 側 WS server) が起動完了
# - device available  = ESP32 が WS server に接続して hello 完了
# この 2 つは別 timing で、 ESP32 boot + WiFi assoc + WS handshake で
# 通常数秒〜十数秒のラグがある。 server ready で起こされた後、 この window
# 内は短間隔 retry で ESP32 の接続を即時検出 (= 起動応答性)、 window を
# 過ぎたら ESP32 が居ない / 永久未接続な状況 (= 電源 off / WiFi 不通 /
# 認証失敗) と判断して通常 polling 間隔に落とし負荷を下げる。
_INITIAL_BURST_SEC = 60.0
_INITIAL_BURST_INTERVAL_SEC = 2.0
# event 経由の device ready 通知が永久に来なかった場合の fallback。
# manager の on_server_ready hook 機構自体が壊れた / 古い本体 (= hook 未対応)
# 等の安全網として、 この時間を過ぎたら polling だけで初期同期も試みる。
_READY_EVENT_FALLBACK_SEC = 120.0

# device ready 通知 (gateway 接続完了) を thread 間で受け渡すための event。
# MCP client の on_server_ready callback が ``set()``、 reconcile loop の
# initial 同期パスが ``wait()`` で受ける。
_device_ready_event = threading.Event()


def _query_current_vessel_persona(vessel_bid: str) -> Optional[str]:
    """DB から「現在 vessel building に入室中のペルソナ ID」を 1 つ取得する。

    Vessel Building は capacity=1 設計なので最大 1 件。 ``BuildingOccupancyLog``
    で ``EXIT_TIMESTAMP IS NULL`` (= 退室記録なし = まだ入室中) を探す。

    DB アクセス失敗時 (起動直後で DB 未初期化等) は WARNING を残して
    ``None`` を返す。 呼び出し元は何もしない (= 次回 tick まで wait)。
    """
    try:
        from database.session import SessionLocal  # noqa: E402
        from database.models import BuildingOccupancyLog  # noqa: E402
    except Exception as exc:
        LOGGER.warning(
            "avatar_loader: cannot import database session: %s", exc
        )
        return None

    db = SessionLocal()
    try:
        row = (
            db.query(BuildingOccupancyLog)
            .filter(BuildingOccupancyLog.BUILDINGID == vessel_bid)
            .filter(BuildingOccupancyLog.EXIT_TIMESTAMP.is_(None))
            .order_by(BuildingOccupancyLog.ID.desc())
            .first()
        )
        return row.AIID if row is not None else None
    except Exception as exc:
        LOGGER.warning(
            "avatar_loader: DB query for current vessel persona failed: %s",
            exc,
        )
        return None
    finally:
        db.close()


def _reconcile_vessel_state(reason: str) -> None:
    """vessel に現在居るペルソナを取得して、 ``on_persona_entered_building``
    相当の avatar transfer 経路に乗せる。 既存 hook を直接呼ぶことでロジック
    重複を避ける (cache check / load / face reset すべて再利用)。
    """
    vessel_bid = _vessel_building_id()
    if not vessel_bid:
        return
    persona_id = _query_current_vessel_persona(vessel_bid)
    if persona_id is None:
        LOGGER.debug(
            "avatar_loader: %s — no persona currently in vessel, nothing to sync",
            reason,
        )
        return
    LOGGER.info(
        "avatar_loader: %s — re-syncing avatar for persona=%s in vessel %s",
        reason, persona_id, vessel_bid,
    )
    try:
        on_persona_entered_building(
            persona_id=persona_id, building_id=vessel_bid,
        )
    except Exception:
        LOGGER.exception(
            "avatar_loader: re-sync via on_persona_entered_building failed "
            "for persona=%s", persona_id,
        )


def _on_stackchan_mcp_ready() -> None:
    """本体 MCP client の ``on_server_ready`` 経由で呼ばれる。 gateway
    subprocess が起動 + 接続完了したタイミングで fire。

    callback は MCP event loop thread から呼ばれるので、 ここでは event を
    起こすだけで重い処理 (session_id 取得 / DB 検索 / avatar transfer) は
    reconcile thread に委譲する。
    """
    LOGGER.info(
        "avatar_loader: stackchan MCP server ready, signaling reconcile thread"
    )
    _device_ready_event.set()


def _register_mcp_ready_hook() -> None:
    """本体 MCP client に on_server_ready hook を register する。

    addon load 時点では ``get_mcp_manager()`` が ``None`` のことが多い
    (= MCP 初期化は main.py の startup シーケンスで avatar_loader の load
    後に走る)。 manager が利用可能になるまで短間隔で polling して、 取れた
    時点で hook を register する。 一定時間経っても manager が来なければ
    諦め、 reconcile_loop の fallback (= タイムアウト後の polling) に
    任せる。
    """
    import tools.mcp_client as _mcp

    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        manager = _mcp.get_mcp_manager()
        if manager is not None and hasattr(manager, "on_server_ready"):
            manager.on_server_ready(
                MCP_QUALIFIED_SERVER, _on_stackchan_mcp_ready,
            )
            LOGGER.info(
                "avatar_loader: registered on_server_ready hook for %s",
                MCP_QUALIFIED_SERVER,
            )
            return
        time.sleep(0.5)
    LOGGER.warning(
        "avatar_loader: MCP manager did not initialize within 60s "
        "(or lacks on_server_ready support), event-driven device ready "
        "skipped — reconcile loop will fall back to polling after %.0fs",
        _READY_EVENT_FALLBACK_SEC,
    )


def _periodic_reconcile_loop() -> None:
    """daemon thread main。 設計:

    1. ``_device_ready_event`` を ``_READY_EVENT_FALLBACK_SEC`` 上限で wait
       (= MCP gateway subprocess の起動完了通知)。
    2. event 起きたら burst window 開始: ``_INITIAL_BURST_SEC`` 秒間、
       ``_INITIAL_BURST_INTERVAL_SEC`` 間隔で ``_fetch_device_session_id``
       を retry (= ESP32 の WS 接続完了を素早く検知)。
    3. session_id が取れたら initial reconcile を 1 回実行。
    4. 以降は ``_POLL_INTERVAL_SEC`` 間隔で session_id 監視 (= シナリオ A
       対応の device reboot 検知)。 burst window 経過後に session_id が
       取れない場合も同じ長間隔に落とす (= ESP32 未接続時の負荷削減)。

    event が timeout までに来なかった場合は burst をスキップして直接長間隔
    polling に入る (= hook 機構が壊れた / 古い本体で hook 非対応 / gateway
    起動失敗 等の最終保険)。
    """
    try:
        got_event = _device_ready_event.wait(timeout=_READY_EVENT_FALLBACK_SEC)
        if not got_event:
            LOGGER.warning(
                "avatar_loader: device ready event not received within %.0fs, "
                "proceeding with polling-only mode",
                _READY_EVENT_FALLBACK_SEC,
            )
            burst_deadline = 0.0  # burst 無効、 即長間隔
        else:
            burst_deadline = time.monotonic() + _INITIAL_BURST_SEC
            LOGGER.info(
                "avatar_loader: MCP ready, entering ESP32 connection burst "
                "window for %.0fs (interval=%.1fs)",
                _INITIAL_BURST_SEC, _INITIAL_BURST_INTERVAL_SEC,
            )

        initial_done = False
        while True:
            current_sid = _fetch_device_session_id()
            if current_sid is None:
                # device 未接続 / 古い firmware / 通信エラー。 burst window
                # 内なら短間隔で連続 retry、 外なら長間隔に落とす。
                in_burst = time.monotonic() < burst_deadline
                time.sleep(
                    _INITIAL_BURST_INTERVAL_SEC if in_burst
                    else _POLL_INTERVAL_SEC
                )
                continue

            if not initial_done:
                # device が初めて ready になった瞬間 — シナリオ B 対応の
                # initial reconcile を実行。 session_id を記録した後で
                # vessel state を sync する。
                LOGGER.info(
                    "avatar_loader: device ready (session=%s), "
                    "performing initial reconcile",
                    current_sid,
                )
                get_avatar_loader().reconcile_session(current_sid)
                _reconcile_vessel_state("initial reconcile (startup)")
                initial_done = True
            else:
                # 以降は session_id 変化検知のみ。 変化があれば cache
                # invalidate + vessel state 再 sync。
                get_avatar_loader().reconcile_session(
                    current_sid,
                    on_invalidate=lambda: _reconcile_vessel_state(
                        "device reboot detected via polling"
                    ),
                )
            time.sleep(_POLL_INTERVAL_SEC)
    except Exception:
        LOGGER.exception(
            "avatar_loader: periodic reconcile loop crashed, "
            "device reboot detection disabled"
        )


# プロセス全体で 1 つだけ thread を動かすために、 thread 名で enumerate
# する。 avatar_loader.py は addon_loader 経由 / addon 内 lazy import 経由
# など複数の module 名で同時 import されうるが、 OS process は同一なので
# ``threading.enumerate()`` で見える thread は共通。
_RECONCILE_THREAD_NAME = "saiverse-avatar-loader-reconcile"
_HOOK_REGISTRAR_THREAD_NAME = "saiverse-avatar-loader-hook-registrar"


def _start_reconcile_thread() -> None:
    """module ロード時に daemon thread を 2 つ起動する。 プロセス内で同名
    thread が既に走っていれば skip (= 多重 import 対策)。

    - reconcile thread: device ready event を待って initial reconcile を
      実行、 以降は session_id polling で reboot 検知。
    - registrar thread: MCP manager 利用可能になるのを待って
      on_server_ready hook を register する (1 回限り)。
    """
    existing_names = {t.name for t in threading.enumerate() if t.is_alive()}
    if _RECONCILE_THREAD_NAME in existing_names:
        LOGGER.debug(
            "avatar_loader: reconcile thread already running, skip"
        )
        return

    threading.Thread(
        target=_register_mcp_ready_hook,
        name=_HOOK_REGISTRAR_THREAD_NAME,
        daemon=True,
    ).start()

    threading.Thread(
        target=_periodic_reconcile_loop,
        name=_RECONCILE_THREAD_NAME,
        daemon=True,
    ).start()

    LOGGER.info(
        "avatar_loader: reconcile thread started "
        "(event_fallback=%.0fs, poll_interval=%.0fs)",
        _READY_EVENT_FALLBACK_SEC, _POLL_INTERVAL_SEC,
    )


_start_reconcile_thread()
