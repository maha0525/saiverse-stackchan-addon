"""M5Stack Ultrasonic Distance Unit I2C (RCWL-9620) ドライバ ― Port A 経由で
目の前の物体までの距離を測る native tool。

Stack-chan の Grove Port A に接続した M5Stack 超音波測距ユニット (RCWL-9620、
I2C addr 0x57) で距離を測定する。 内部で stackchan-mcp の汎用 I2C tool
(PR ②、 ``i2c_write`` / ``i2c_read``) を呼び、 プロトコルの解釈は本ファイル内で
完結させる (= env3.py / servo8.py と同じ構図、 生 i2c をペルソナに晒さない)。

測定プロトコル (M5Stack 公式 Arduino ライブラリ ``m5stack/M5Unit-Sonic`` の
``SONIC_I2C::getDistance`` を真として移植):

  1. addr 0x57 に 1 byte ``0x01`` を write (= 測距トリガ)。
  2. 約 120 ms 待機 (= 超音波の往復 + 内部処理。 公式 lib の ``delay(120)``)。
     最大レンジ 450 cm の往復 = 約 26 ms だが、 公式の 120 ms に合わせる。
  3. 3 byte を read。 ``raw = (b0 << 16) | (b1 << 8) | b2`` の 24-bit 値で、
     単位は µm。 ``distance_mm = raw / 1000`` (= 公式 lib の ``float(data)/1000``)。
  4. 4500 mm (= 450 cm) でクランプ (公式 lib と同じ上限)。

この「write → 待機 → read」 の 3 段は SHT30 (env3.py) と同じ理由で
``i2c_write_read`` 1 発に畳めない (Repeated Start では master 側で測定待ちの
120 ms を挟めず、 測定完了前に read して 0xFF / 不定値が返る。 詳細は
``tools/units/README.md`` の Pitfall)。

測定対象 / レンジ: 正面 60° の指向角内で最も近い物体までの距離。 2 cm 〜
450 cm、 精度 ±2% (datasheet)。

可視化フロー (v0.10 マルチ機体): 機体ごとの capability (vessels.db の
``sonic``、 機体管理 UI で手動設定) が True の機体に降りたペルソナにだけ
spell として公開される (intent K-5、 不変条件 #14)。 物理 Unit が無い機体
では capability を OFF にしておくことで「効かない tool」 が LLM に出ない。
旧 addon 単一 ``unit_sonic_enabled`` toggle は廃止。

戻り値型: native tool は ``str`` を返す (SEA runtime は str / (str, dict) の
2 形式しか正規対応しない。 詳細: docs/issues/native_tool_return_4tuple_bug.md)。
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from tools.core import ToolSchema

# addon 内 ``tools/hubs/pahub.py`` を import するため、 addon の tools/
# (= 親ディレクトリ) を sys.path に通す。 env3.py / servo8.py と同じ事情
# (loader は ``tools/units/`` しか積まないので 1 段上を追加する)。
_ADDON_TOOLS_DIR = str(Path(__file__).resolve().parent.parent)
if _ADDON_TOOLS_DIR not in sys.path:
    sys.path.insert(0, _ADDON_TOOLS_DIR)
# addon root (vessel_dispatch.py) も import 可能にする (units/ から 2 段上)。
_ADDON_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _ADDON_ROOT not in sys.path:
    sys.path.insert(0, _ADDON_ROOT)

from vessel_dispatch import resolve_vessel_connection  # noqa: E402

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"
MCP_QUALIFIED_SERVER = f"{ADDON_NAME}__stackchan"
MCP_TOOL_WRITE = "i2c_write"
MCP_TOOL_READ = "i2c_read"

# このユニットの capability キー (vessels.db の per-vessel capabilities で使う名前)。
# ``vessel_dispatch.reregister_unit_tools`` は「``MY_UNIT_CAP_KEY`` を持つ = 再登録
# 対象のユニット tool」の識別マーカーとしてこれを見る (README tools/units/ 規約)。
MY_UNIT_CAP_KEY = "sonic"

# --- M5Stack Ultrasonic Distance Unit I2C (RCWL-9620) ---
SONIC_ADDR = 0x57
SONIC_CMD_MEASURE = [0x01]   # 測距トリガ (公式 lib は 1 byte 0x01 を write)
SONIC_RESULT_BYTES = 3       # 24-bit raw distance (µm)
# 公式 lib の delay(120)。 最大レンジ往復 (~26 ms) + 内部処理の余裕。
SONIC_MEASURE_WAIT_SEC = 0.120
# 公式 lib のクランプ上限 (= 450 cm)。 datasheet の測距レンジ上限と一致。
SONIC_MAX_MM = 4500.0
# RCWL-9620 は遅いユニットで、 Port A 汎用 i2c tool の既定 400 kHz では通信が
# 破綻する。 各 i2c 呼び出しで scl_speed_hz を明示的に下げる (= firmware 側の
# optional scl_speed_hz property。 未対応の旧 firmware では無視されるので
# firmware 更新が前提)。
#
# 速度は 100 kHz。 実機検証 (PaHUB ch5 単独 / 全 channel 開放いずれでも) で判明:
#   - trigger write (master が SDA 駆動) は 200 kHz でも ACK する
#   - 測定値 read (slave が SDA 駆動、 ESP-IDF i2c_master_receive がサンプリング)
#     は 200 kHz だと ESP_ERR_INVALID_STATE / 不定値で erratic、 100 kHz で
#     初めて安定 (5/5 同値)。 read 経路が律速。
# write も合わせて 100 kHz にする (slower は write には常に安全側)。 M5 公式 lib は
# 200 kHz だが、 これは Arduino Wire 経由の話で、 ESP-IDF i2c_master + PaHUB 経由の
# read には 100 kHz が要る。
SONIC_SCL_SPEED_HZ = 100000

_DEFAULT_TIMEOUT_SEC = 10.0


def _unit_present() -> bool:
    """現在ペルソナが降りている機体が超音波測距ユニットを搭載しているか。

    機体ごとの capability (vessels.db の ``sonic``、 機体管理 UI で手動設定)
    を見る。 旧 addon 単一 ``unit_sonic_enabled`` toggle を置換 (intent K-5、
    不変条件 #14)。 ペルソナ context が無い / 機体が解決できない場合は False
    (= 安全側、 搭載なし扱い)。 スキーマ可視性 (``_build_schema``) と同じ
    capability を参照する。
    """
    from vessel_dispatch import (
        VesselNotAvailable,
        resolve_vessel,
        units_of_type,
    )

    try:
        vessel = resolve_vessel()
    except VesselNotAvailable:
        return False
    # 配置 (unit_config) があればそれ、 無ければ legacy capabilities に基づく
    return bool(units_of_type(vessel, MY_UNIT_CAP_KEY))


def _get_mcp_connection():
    # 現在ペルソナが降りている機体の gateway インスタンスを解決する
    # (vessel_dispatch、 intent K-4)。複数機体の同時稼働では機体ごとに別
    # gateway なので、 ここで「いまその身体が降りている機体」へ i2c を向ける。
    _vessel, conn = resolve_vessel_connection()
    return conn


async def _call_i2c_write(addr: int, write_bytes: List[int]) -> str:
    conn = _get_mcp_connection()
    return await conn.call_tool(
        MCP_TOOL_WRITE,
        {"addr": addr, "bytes": write_bytes, "scl_speed_hz": SONIC_SCL_SPEED_HZ},
    )


async def _call_i2c_read(addr: int, n_bytes: int) -> str:
    conn = _get_mcp_connection()
    return await conn.call_tool(
        MCP_TOOL_READ,
        {"addr": addr, "n_bytes": n_bytes, "scl_speed_hz": SONIC_SCL_SPEED_HZ},
    )


def _parse_i2c_payload(rendered: str) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(rendered)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _run_on_mcp_loop(coro, timeout_sec: float = _DEFAULT_TIMEOUT_SEC) -> Any:
    import tools.mcp_client as _mcp

    loop = _mcp._loop
    if loop is None:
        # 未スケジュールの coroutine を閉じて "never awaited" 警告を防ぐ。この分岐は
        # MCP 未起動時のみ通る (schedule 後は loop 所有なので coro には触らない)。
        coro.close()
        raise RuntimeError("MCP event loop is not initialized")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout_sec)


# ============================================================
# Hub channel select + lazy recovery (env3.py / servo8.py と同じ方針)
# ============================================================
# 各 i2c 操作の前に対象ユニットの channel をバスに出す。 共通実装 (channel 解決 +
# select + i2c-level error 時の再ルーティング再試行) は
# ``hubs.pahub.execute_with_hub_recovery`` に集約。 本ファイルは i2c-level failure
# 判定の述語だけを持ち、 薄いラッパで共通実装に渡す。 直結 (hub_type=none) なら
# 機構自体スキップして既存挙動と等価。

def _payload_has_esp_err(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("ok"):
        return False
    err = payload.get("error", "")
    return isinstance(err, str) and err.startswith("ESP_ERR_")


def _sonic_is_i2c_failure(
    result: Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]],
) -> bool:
    write_payload, read_payload = result
    return _payload_has_esp_err(write_payload) or _payload_has_esp_err(read_payload)


async def _execute_with_hub_recovery(
    operation: Callable[[], Awaitable[Any]],
    is_i2c_failure: Callable[[Any], bool],
) -> Any:
    """このユニット (sonic) の i2c 操作を channel select + lazy recovery 付きで実行。

    共通実装は ``pahub.execute_with_hub_recovery``。channel は現在 vessel の sonic
    配置から解決される (docs/intent/stackchan_unit_placement.md §4)。
    """
    from hubs.pahub import execute_with_hub_recovery

    return await execute_with_hub_recovery(
        operation, is_i2c_failure, MY_UNIT_CAP_KEY
    )


# ============================================================
# 測距シーケンス
# ============================================================

async def _measure_sonic() -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """測距トリガ → 120 ms 待機 → 3 byte read の 1-shot 測定。"""
    write_rendered = await _call_i2c_write(SONIC_ADDR, SONIC_CMD_MEASURE)
    write_payload = _parse_i2c_payload(write_rendered)
    if write_payload is None or not write_payload.get("ok"):
        return write_payload, None
    await asyncio.sleep(SONIC_MEASURE_WAIT_SEC)
    read_rendered = await _call_i2c_read(SONIC_ADDR, SONIC_RESULT_BYTES)
    read_payload = _parse_i2c_payload(read_rendered)
    return write_payload, read_payload


def get_sonic_distance() -> str:
    """超音波測距ユニットで正面の物体までの距離を測って返す。

    RCWL-9620 に測距コマンド (0x01) を送り → 約 120 ms 待機 → 3 byte
    (24-bit µm) を read。 ``distance_mm = raw / 1000`` で mm に換算し、
    450 cm でクランプ (= M5Stack 公式 lib 準拠)。

    Returns:
        距離を整形した日本語文字列、 もしくはエラーメッセージ。
    """
    if not _unit_present():
        return (
            "この身体 (Stack-chan) には超音波距離センサー (RCWL-9620) が"
            " 搭載されていません。 搭載機体なら機体管理 UI で「超音波距離"
            "センサー」 を ON にしてください。"
        )

    try:
        write_payload, read_payload = _run_on_mcp_loop(
            _execute_with_hub_recovery(_measure_sonic, _sonic_is_i2c_failure)
        )
    except Exception as exc:
        LOGGER.exception("sonic.distance: measurement sequence failed")
        return f"超音波測距ユニットの測定に失敗しました (I2C 通信エラー): {exc}"

    if write_payload is None or not write_payload.get("ok"):
        err = (
            write_payload.get("error", "unknown")
            if isinstance(write_payload, dict)
            else "no response"
        )
        LOGGER.warning("sonic.distance: measure command write failed: %s", err)
        return (
            f"超音波測距ユニットに測定コマンドを送信できませんでした: {err}。 "
            "Port A への Unit 接続状態を確認してください。"
        )

    if read_payload is None:
        LOGGER.warning("sonic.distance: read returned non-JSON")
        return "超音波測距ユニットの測定値読み取り応答を解釈できませんでした。"

    if not read_payload.get("ok"):
        err = read_payload.get("error", "unknown")
        LOGGER.warning("sonic.distance: read failed: %s", err)
        return (
            f"超音波測距ユニットから測定値を取得できませんでした: {err}。 "
            "Port A への Unit 接続状態を確認してください。"
        )

    raw_bytes = read_payload.get("bytes")
    if not isinstance(raw_bytes, list) or len(raw_bytes) != SONIC_RESULT_BYTES:
        LOGGER.warning("sonic.distance: unexpected bytes payload: %r", raw_bytes)
        return (
            f"超音波測距ユニットから想定外のバイト列が返却されました "
            f"(期待 {SONIC_RESULT_BYTES} byte、 実測 {raw_bytes})。"
        )

    raw_um = (raw_bytes[0] << 16) | (raw_bytes[1] << 8) | raw_bytes[2]
    distance_mm = raw_um / 1000.0
    # 公式 lib は ``Distance > 4500.00`` のときだけ 4500 にクランプ
    # (= 厳密に超過。 ちょうど 450 cm の正規読み値はそのまま報告する)。
    # このセンチネル値 (典型的には 0xFFFFFF) は「測定不能」 を意味し、 物体が無い /
    # 遠すぎる (>450 cm) 場合だけでなく、 逆に近すぎる (最小レンジ約 2 cm 未満〜接触)
    # 場合にも返る (実機確認)。 値からは遠い/近いを区別できないので、 メッセージは
    # 両端を併記する。
    out_of_range = distance_mm > SONIC_MAX_MM
    if out_of_range:
        distance_mm = SONIC_MAX_MM
    distance_cm = distance_mm / 10.0

    LOGGER.info(
        "sonic.distance: %.1f cm (raw=0x%06X = %d µm%s)",
        distance_cm,
        raw_um,
        raw_um,
        ", out of range (sentinel)" if out_of_range else "",
    )

    if out_of_range:
        return (
            "距離: 測定レンジ外 (測定可能なのは約 2〜450 cm、 超音波測距ユニット / "
            "RCWL-9620、Stack-chan Port A)。 正面に反射する物体が無い / 450 cm より"
            "遠い場合だけでなく、 逆に物体が近すぎる (約 2 cm 未満〜接触) 場合も同じ"
            "判定になり、 この値からはどちらかを区別できません。"
        )

    return (
        f"距離: {distance_cm:.1f} cm (正面の物体まで、 超音波測距ユニット / "
        f"RCWL-9620、Stack-chan Port A)。"
    )


# ============================================================
# Spell registry
# ============================================================

def _build_schema(name: str, description: str, display_name: str) -> ToolSchema:
    # 複数機体 (intent 不変条件 #14 ユニット側): 超音波測距ユニットを積んだ機体の
    # Vessel Building でだけ visible。capability は vessels.db の per-vessel 値
    # (機体管理 UI で手動設定)。単一 unit_sonic_enabled toggle をやめる。
    from vessel_dispatch import building_gate_or_hidden, list_building_ids_with_capability

    building_ids, visible = building_gate_or_hidden(
        list_building_ids_with_capability(MY_UNIT_CAP_KEY)
    )
    return ToolSchema(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}, "required": []},
        result_type="string",
        spell=True,
        spell_display_name=display_name,
        spell_visible=visible,
        building_ids=building_ids,
    )


def schemas() -> List[ToolSchema]:
    """1 ファイル複数 spell の登録 entry point (env3.py / servo8.py と同形)。

    ``spell_visible`` / ``building_ids`` は sonic capability を持つ機体の
    Vessel Building 集合から決まる (= ``list_building_ids_with_capability``)。
    schemas() は spell surface 構築のたびに呼ばれるので、 機体管理 UI で
    capability を切り替えた後の reconnect で即時に visibility が反映される
    (= subprocess restart 不要)。
    """
    from vessel_dispatch import list_building_ids_with_capability

    if not list_building_ids_with_capability(MY_UNIT_CAP_KEY):
        LOGGER.debug(
            "sonic: no vessel declares sonic capability; spells hidden until "
            "set in 機体管理 UI"
        )

    return [
        _build_schema(
            name="get_sonic_distance",
            description=(
                "あなたの身体 (Stack-chan) に接続された M5Stack 超音波測距ユニット"
                " (RCWL-9620) で、 正面にある物体までの距離 (cm) を測る。 指向角"
                " およそ 60°、 測定可能なのは約 2〜450 cm。 「近づいてきた」「目の前に"
                " 何かある」「どのくらい離れている」 等の空間把握の根拠に使える。"
                " 物体が無い/遠すぎる場合と、 逆に近すぎる (約 2 cm 未満〜接触) 場合は"
                " どちらも「レンジ外」 となり、 値からは区別できない点に注意。"
            ),
            display_name="距離を測る",
        ),
    ]
