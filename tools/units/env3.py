"""M5Stack ENV III Unit ドライバ (温湿度: SHT30 0x44 / 気圧: QMP6988 0x70)。

Stack-chan の Grove Port A に接続した ENV III Unit から温湿度・気圧を取得
する native tool 群。 内部で stackchan-mcp の汎用 I2C tool (PR ②、
``self.i2c.write`` / ``self.i2c.read``) を呼び出し、 chip 固有の生バイト
解釈は本ファイル内で行う。 ペルソナは「温度教えて」 「気圧教えて」 と呼ぶ
だけで具体的な数値が返る。

有効化フロー: AddonConfig の ``unit_env3_enabled`` を true にすると spell
として公開される (= addon UI の toggle で ON/OFF)。 物理 Unit が無い時は
無効化したままにすることで「効かない tool」 が LLM に出ないようにする。

戻り値型: native tool は ``str`` を返す。 SEA runtime
(``sea/runtime_llm.py:_run_spell_tool_async``) は ``str`` または
``(str, dict)`` の 2-tuple しか正規対応していないため、 4-tuple を返すと
str() 化されて LLM に tuple repr 文字列が渡る (詳細:
``docs/issues/native_tool_return_4tuple_bug.md``)。

実装ソース: SHT30 = Sensirion 公式データシート (cmd 0x2C06、 高精度モード、
15 ms wait)。 QMP6988 = M5Stack 公式 Arduino ライブラリ ``M5Unit-ENV``
(QMP6988.h / QMP6988.cpp)。 calibration K-values と compensation 式は
m5stack/M5Unit-ENV から 1:1 で Python に移植している。
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from tools.core import ToolSchema

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"
MCP_QUALIFIED_SERVER = f"{ADDON_NAME}__stackchan"
MCP_TOOL_WRITE = "i2c_write"
MCP_TOOL_READ = "i2c_read"

# --- SHT30 (温湿度、 0x44) ---
SHT30_ADDR = 0x44
SHT30_CMD_MEASURE_HIGH_REP = [0x2C, 0x06]
SHT30_RESULT_BYTES = 6
# SHT30 high-repeatability mode requires ~15 ms for measurement. Use 20 ms
# to absorb scheduling jitter. Must NOT be folded into a single
# i2c_write_read (Repeated Start) transaction — empirically returns 0xFF
# sentinels when read before the measurement completes.
SHT30_MEASURE_WAIT_SEC = 0.020

# --- QMP6988 (気圧、 0x70) ---
QMP6988_ADDR = 0x70
QMP6988_REG_CHIP_ID = 0xD1
QMP6988_REG_RESET = 0xE0
QMP6988_REG_CONFIG = 0xF1
QMP6988_REG_STATUS = 0xF3
QMP6988_REG_CTRLMEAS = 0xF4
QMP6988_REG_PRESSURE_MSB = 0xF7  # F7-F9 = pressure raw、 FA-FC = temp raw
QMP6988_REG_CALIB_START = 0xA0
QMP6988_CALIB_LENGTH = 25

QMP6988_CHIP_ID = 0x5C
QMP6988_SUBTRACTOR = 8388608  # = 0x800000、 raw 24-bit unsigned -> signed の offset

# Power mode (low 2 bits of ctrl_meas)
QMP6988_POWERMODE_SLEEP = 0x00
QMP6988_POWERMODE_FORCED = 0x01
QMP6988_POWERMODE_NORMAL = 0x03

# Oversampling
QMP6988_OSR_SKIP = 0x00
QMP6988_OSR_1X = 0x01
QMP6988_OSR_2X = 0x02
QMP6988_OSR_4X = 0x03
QMP6988_OSR_8X = 0x04
QMP6988_OSR_16X = 0x05

# osr_t (5..7) | osr_p (2..4) | mode (0..1) で ctrl_meas を構成
# Normal-mode と異なり forced-mode は 1-shot で sleep に戻る
QMP6988_CTRLMEAS_FORCED = (
    (QMP6988_OSR_1X << 5) | (QMP6988_OSR_8X << 2) | QMP6988_POWERMODE_FORCED
)
# osr_t=1x + osr_p=8x で typ 37.9 ms (datasheet)。 50 ms 待ち + status は
# poll しない (= 簡略化)
QMP6988_MEASURE_WAIT_SEC = 0.050

_DEFAULT_TIMEOUT_SEC = 10.0

# Module-level cache: device は再起動するまで calibration data が変わらない
# ため、 1 回読んだら以降は cache を使う。 Stack-chan の reboot を跨いだ
# キャッシュ無効化は cache 値の妥当性次第 (= 同じチップなら不変)。
_qmp6988_ik_cache: Optional[Dict[str, int]] = None


def _addon_params() -> Dict[str, Any]:
    try:
        from saiverse.addon_config import get_params

        return get_params(ADDON_NAME) or {}
    except Exception:
        LOGGER.exception("env3: failed to load AddonConfig params")
        return {}


def _vessel_building_id() -> Optional[str]:
    vbid = _addon_params().get("vessel_building_id")
    return str(vbid) if vbid else None


def _unit_enabled() -> bool:
    val = _addon_params().get("unit_env3_enabled", False)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes", "on")
    return bool(val)


def _get_mcp_connection():
    from tools.mcp_client import _make_instance_key, get_mcp_manager

    manager = get_mcp_manager()
    if manager is None:
        raise RuntimeError("MCP manager is not initialized")
    instance_key = _make_instance_key(MCP_QUALIFIED_SERVER, persona_id=None)
    conn = manager._connections.get(instance_key)
    if conn is None:
        raise RuntimeError(
            f"MCP server '{MCP_QUALIFIED_SERVER}' is not connected. "
            "Make sure the stackchan-mcp gateway is running and paired."
        )
    return conn


async def _call_i2c_write(addr: int, write_bytes: list[int]) -> str:
    """Send a command / register pointer to a Port A I2C device."""
    conn = _get_mcp_connection()
    return await conn.call_tool(
        MCP_TOOL_WRITE, {"addr": addr, "bytes": write_bytes}
    )


async def _call_i2c_read(addr: int, n_bytes: int) -> str:
    """Read raw bytes from a Port A I2C device's current output register."""
    conn = _get_mcp_connection()
    return await conn.call_tool(
        MCP_TOOL_READ, {"addr": addr, "n_bytes": n_bytes}
    )


def _parse_i2c_payload(rendered: str) -> Optional[Dict[str, Any]]:
    """gateway 経由の i2c_* tool レスポンス JSON をパース。"""
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
        raise RuntimeError("MCP event loop is not initialized")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout_sec)


# ============================================================
# SHT30 (温湿度、 0x44)
# ============================================================

async def _measure_sht30() -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Run the SHT30 high-repeatability measurement sequence."""
    write_rendered = await _call_i2c_write(SHT30_ADDR, SHT30_CMD_MEASURE_HIGH_REP)
    write_payload = _parse_i2c_payload(write_rendered)
    if write_payload is None or not write_payload.get("ok"):
        return write_payload, None
    await asyncio.sleep(SHT30_MEASURE_WAIT_SEC)
    read_rendered = await _call_i2c_read(SHT30_ADDR, SHT30_RESULT_BYTES)
    read_payload = _parse_i2c_payload(read_rendered)
    return write_payload, read_payload


def get_env3_temperature_humidity() -> str:
    """ENV III (SHT30) から温度・湿度を取得して返す。

    SHT30 の高精度モード測定 (cmd 0x2C06) を発行 → 約 15ms 待機 → 6 bytes
    (T_msb, T_lsb, T_crc, H_msb, H_lsb, H_crc) を read。 温度・湿度の式は
    Sensirion 公式データシート §4.13 準拠:

      T[°C] = -45 + 175 * raw_T / 65535
      H[%]  = 100 * raw_H / 65535

    CRC 検証は本版では省略 (= まずは動作優先)。

    Returns:
        温度・湿度を整形した日本語文字列、 もしくはエラーメッセージ。
    """
    if not _unit_enabled():
        return (
            "ENV III は無効化されています。 アドオン管理 UI で「ENV III "
            "(温湿度・気圧) を有効化」 を ON にしてください。"
        )

    try:
        write_payload, read_payload = _run_on_mcp_loop(_measure_sht30())
    except Exception as exc:
        LOGGER.exception("env3.temperature_humidity: SHT30 sequence failed")
        return f"ENV III の測定に失敗しました (I2C 通信エラー): {exc}"

    if write_payload is None or not write_payload.get("ok"):
        err = (
            write_payload.get("error", "unknown")
            if isinstance(write_payload, dict)
            else "no response"
        )
        LOGGER.warning(
            "env3.temperature_humidity: SHT30 command write failed: %s", err
        )
        return (
            f"ENV III に測定コマンドを送信できませんでした: {err}。 "
            "Port A への Unit 接続状態を確認してください。"
        )

    if read_payload is None:
        LOGGER.warning(
            "env3.temperature_humidity: SHT30 read returned non-JSON"
        )
        return "ENV III の測定値読み取り応答を解釈できませんでした。"

    if not read_payload.get("ok"):
        err = read_payload.get("error", "unknown")
        LOGGER.warning(
            "env3.temperature_humidity: SHT30 read failed: %s", err
        )
        return (
            f"ENV III から測定値を取得できませんでした: {err}。 "
            "Port A への Unit 接続状態を確認してください。"
        )

    raw_bytes = read_payload.get("bytes")
    if not isinstance(raw_bytes, list) or len(raw_bytes) != SHT30_RESULT_BYTES:
        LOGGER.warning(
            "env3.temperature_humidity: unexpected bytes payload: %r",
            raw_bytes,
        )
        return (
            f"ENV III から想定外のバイト列が返却されました "
            f"(期待 6 byte、 実測 {raw_bytes})。"
        )

    t_raw = (raw_bytes[0] << 8) | raw_bytes[1]
    h_raw = (raw_bytes[3] << 8) | raw_bytes[4]
    temperature_c = -45.0 + 175.0 * t_raw / 65535.0
    humidity_pct = 100.0 * h_raw / 65535.0

    LOGGER.info(
        "env3.temperature_humidity: T=%.2f°C H=%.2f%% (raw T=0x%04X H=0x%04X)",
        temperature_c,
        humidity_pct,
        t_raw,
        h_raw,
    )

    return (
        f"温度: {temperature_c:.1f}°C、湿度: {humidity_pct:.1f}% "
        f"(ENV III / SHT30、Stack-chan Port A)。"
    )


# ============================================================
# QMP6988 (気圧、 0x70)
# ============================================================

def _sign_extend(value: int, bits: int) -> int:
    """符号付き整数として解釈。 ``bits`` 幅の two's complement。"""
    sign_bit = 1 << (bits - 1)
    if value & sign_bit:
        value -= 1 << bits
    return value


def _parse_qmp6988_calibration(raw: List[int]) -> Dict[str, int]:
    """OTP 25 bytes (= 0xA0..0xB8) から 12 個の calibration coefficient を取得。

    Byte layout は M5Stack 公式実装 (QMP6988.cpp:getCalibrationData) に従う。
    a0 / b00 は 20-bit signed (= byte 24 の上下 nibble と合成)、 他は
    16-bit signed (= big-endian 2 bytes)。
    """
    if len(raw) != QMP6988_CALIB_LENGTH:
        raise ValueError(
            f"QMP6988 calibration data length mismatch: "
            f"expected {QMP6988_CALIB_LENGTH}, got {len(raw)}"
        )

    # a0: bytes[18] (msb, 8bit) | bytes[19] (mid, 8bit) | bytes[24] low nibble
    a0_u20 = (raw[18] << 12) | (raw[19] << 4) | (raw[24] & 0x0F)
    a0 = _sign_extend(a0_u20, 20)

    # b00: bytes[0] (msb) | bytes[1] (mid) | bytes[24] high nibble
    b00_u20 = (raw[0] << 12) | (raw[1] << 4) | ((raw[24] & 0xF0) >> 4)
    b00 = _sign_extend(b00_u20, 20)

    def s16(hi: int, lo: int) -> int:
        return _sign_extend((hi << 8) | lo, 16)

    coe_bt1 = s16(raw[2], raw[3])
    coe_bt2 = s16(raw[4], raw[5])
    coe_bp1 = s16(raw[6], raw[7])
    coe_b11 = s16(raw[8], raw[9])
    coe_bp2 = s16(raw[10], raw[11])
    coe_b12 = s16(raw[12], raw[13])
    coe_b21 = s16(raw[14], raw[15])
    coe_bp3 = s16(raw[16], raw[17])
    coe_a1 = s16(raw[20], raw[21])
    coe_a2 = s16(raw[22], raw[23])

    return {
        "a0": a0,
        "b00": b00,
        "a1": coe_a1,
        "a2": coe_a2,
        "bt1": coe_bt1,
        "bt2": coe_bt2,
        "bp1": coe_bp1,
        "b11": coe_b11,
        "bp2": coe_bp2,
        "b12": coe_b12,
        "b21": coe_b21,
        "bp3": coe_bp3,
    }


def _compute_qmp6988_ik(coe: Dict[str, int]) -> Dict[str, int]:
    """生 calibration coefficient から compensation で使う K-value (ik) に変換。

    M5Stack 公式 QMP6988.cpp:getCalibrationData の後段ロジック。 magic
    定数 (3608L, 1731677965L, ...) は datasheet の Q-format scaling 由来。
    """
    return {
        "a0": coe["a0"],                                    # 20Q4
        "b00": coe["b00"],                                  # 20Q4
        "a1": 3608 * coe["a1"] - 1731677965,                # 31Q23
        "a2": 16889 * coe["a2"] - 87619360,                 # 30Q47
        "bt1": 2982 * coe["bt1"] + 107370906,               # 28Q15
        "bt2": 329854 * coe["bt2"] + 108083093,             # 34Q38
        "bp1": 19923 * coe["bp1"] + 1133836764,             # 31Q20
        "b11": 2406 * coe["b11"] + 118215883,               # 28Q34
        "bp2": 3079 * coe["bp2"] - 181579595,               # 29Q43
        "b12": 6846 * coe["b12"] + 85590281,                # 29Q53
        "b21": 13836 * coe["b21"] + 79333336,               # 29Q60
        "bp3": 2915 * coe["bp3"] + 157155561,               # 28Q65
    }


def _compensate_temperature(ik: Dict[str, int], dt: int) -> int:
    """convTx02e — 生 ΔT から temperature compensation した S16 を返す。

    M5Stack 公式 QMP6988.cpp:convTx02e の Python 移植。 Q-format の整数演算
    で実装。 ``dt`` は raw 24-bit unsigned から SUBTRACTOR を引いた signed
    32-bit 値。 戻り値は temperature * 256 (S8.8 不動小数点 相当)。
    """
    wk1 = ik["a1"] * dt
    wk2 = (ik["a2"] * dt) >> 14
    wk2 = (wk2 * dt) >> 10
    wk2 = ((wk1 + wk2) // 32767) >> 19
    return (ik["a0"] + wk2) >> 4


def _compensate_pressure(ik: Dict[str, int], dp: int, tx: int) -> int:
    """getPressure02e — 生 ΔP + compensated T から pressure 補正値 (Pa * 16) を返す。

    M5Stack 公式 QMP6988.cpp:getPressure02e の Python 移植。 ``dp`` は
    raw 24-bit unsigned - SUBTRACTOR、 ``tx`` は ``_compensate_temperature``
    の戻り値。 戻り値は pressure_pa * 16 (Q4 相当)。
    """
    wk1 = ik["bt1"] * tx
    wk2 = (ik["bp1"] * dp) >> 5
    wk1 += wk2
    wk2 = (ik["bt2"] * tx) >> 1
    wk2 = (wk2 * tx) >> 8
    wk3 = wk2
    wk2 = (ik["b11"] * tx) >> 4
    wk2 = (wk2 * dp) >> 1
    wk3 += wk2
    wk2 = (ik["bp2"] * dp) >> 13
    wk2 = (wk2 * dp) >> 1
    wk3 += wk2
    wk1 += wk3 >> 14
    wk2 = ik["b12"] * tx
    wk2 = (wk2 * tx) >> 22
    wk2 = (wk2 * dp) >> 1
    wk3 = wk2
    wk2 = (ik["b21"] * tx) >> 6
    wk2 = (wk2 * dp) >> 23
    wk2 = (wk2 * dp) >> 1
    wk3 += wk2
    wk2 = (ik["bp3"] * dp) >> 12
    wk2 = (wk2 * dp) >> 23
    wk2 = wk2 * dp
    wk3 += wk2
    wk1 += wk3 >> 15
    wk1 //= 32767
    wk1 >>= 11
    wk1 += ik["b00"]
    return wk1


async def _ensure_qmp6988_calibration_cached() -> Tuple[Optional[Dict[str, int]], Optional[str]]:
    """QMP6988 の calibration を module-level cache に確保。

    Returns ``(ik_dict, error_message)``。 失敗時は ``(None, error_msg)``。
    """
    global _qmp6988_ik_cache
    if _qmp6988_ik_cache is not None:
        return _qmp6988_ik_cache, None

    # 0xA0 から 25 bytes を 1 transaction で read (= i2c_write_read で reg
    # pointer 設定 + 連続 read)。 i2c_write+i2c_read を分けても良いが、
    # write_read のほうが I2C bus 上の wasted time が短い。
    rendered = await _get_mcp_connection().call_tool(
        "i2c_write_read",
        {
            "addr": QMP6988_ADDR,
            "write_bytes": [QMP6988_REG_CALIB_START],
            "n_bytes": QMP6988_CALIB_LENGTH,
        },
    )
    payload = _parse_i2c_payload(rendered)
    if payload is None:
        return None, "QMP6988 calibration 応答を解釈できませんでした。"
    if not payload.get("ok"):
        return None, f"QMP6988 calibration 取得に失敗: {payload.get('error', 'unknown')}"

    raw = payload.get("bytes")
    if not isinstance(raw, list) or len(raw) != QMP6988_CALIB_LENGTH:
        return None, (
            f"QMP6988 calibration から想定外のバイト列 "
            f"(期待 {QMP6988_CALIB_LENGTH} byte、 実測 {raw})。"
        )

    try:
        coe = _parse_qmp6988_calibration(raw)
        ik = _compute_qmp6988_ik(coe)
    except Exception as exc:
        return None, f"QMP6988 calibration の解析に失敗: {exc}"

    _qmp6988_ik_cache = ik
    LOGGER.info(
        "env3.qmp6988: calibration cached "
        "(a0=%d b00=%d a1=%d bt1=%d bp1=%d)",
        ik["a0"], ik["b00"], ik["a1"], ik["bt1"], ik["bp1"],
    )
    return ik, None


async def _measure_qmp6988() -> Tuple[Optional[float], Optional[float], Optional[str]]:
    """QMP6988 の forced-mode 1-shot measurement。

    Returns ``(pressure_pa, temperature_c, error_message)``。 失敗時は
    ``(None, None, error_msg)``、 成功時は ``(pa, c, None)``。
    """
    # 1. calibration を cache に確保 (初回のみ実際の I2C call)
    ik, err = await _ensure_qmp6988_calibration_cached()
    if ik is None:
        return None, None, err

    # 2. ctrl_meas に書き込みで forced-mode 1-shot 開始
    write_rendered = await _call_i2c_write(
        QMP6988_ADDR, [QMP6988_REG_CTRLMEAS, QMP6988_CTRLMEAS_FORCED]
    )
    write_payload = _parse_i2c_payload(write_rendered)
    if write_payload is None or not write_payload.get("ok"):
        err = (
            write_payload.get("error", "unknown")
            if isinstance(write_payload, dict)
            else "no response"
        )
        return None, None, f"QMP6988 への測定コマンド送信に失敗: {err}"

    # 3. measurement 完了まで wait (= osr_t=1x + osr_p=8x で typ 37.9 ms)
    await asyncio.sleep(QMP6988_MEASURE_WAIT_SEC)

    # 4. raw P + T を 0xF7 から 6 bytes で読む
    read_rendered = await _get_mcp_connection().call_tool(
        "i2c_write_read",
        {
            "addr": QMP6988_ADDR,
            "write_bytes": [QMP6988_REG_PRESSURE_MSB],
            "n_bytes": 6,
        },
    )
    read_payload = _parse_i2c_payload(read_rendered)
    if read_payload is None or not read_payload.get("ok"):
        err = (
            read_payload.get("error", "unknown")
            if isinstance(read_payload, dict)
            else "no response"
        )
        return None, None, f"QMP6988 から測定値を取得できませんでした: {err}"

    raw = read_payload.get("bytes")
    if not isinstance(raw, list) or len(raw) != 6:
        return None, None, (
            f"QMP6988 から想定外のバイト列 (期待 6 byte、 実測 {raw})。"
        )

    # 5. raw 24-bit unsigned -> signed 32-bit (SUBTRACTOR を引く)
    p_read = (raw[0] << 16) | (raw[1] << 8) | raw[2]
    t_read = (raw[3] << 16) | (raw[4] << 8) | raw[5]
    p_raw = p_read - QMP6988_SUBTRACTOR
    t_raw = t_read - QMP6988_SUBTRACTOR

    # 6. compensation
    t_int = _compensate_temperature(ik, t_raw)
    p_int = _compensate_pressure(ik, p_raw, t_int)
    temperature_c = t_int / 256.0
    pressure_pa = p_int / 16.0

    return pressure_pa, temperature_c, None


def get_env3_air_pressure() -> str:
    """ENV III (QMP6988) から気圧を取得して返す。

    QMP6988 は forced-mode 1-shot で raw pressure / temperature を取得し、
    OTP calibration coefficients (= startup 時 1 回 cache) を使って物理単位
    に補正する。 補正パイプラインは M5Stack 公式実装 (M5Unit-ENV/QMP6988.cpp)
    から 1:1 で Python に移植している。

    気圧は hPa (= Pa / 100) に換算して返す。 一般的な海上補正気圧は
    1013.25 hPa で、 山の上だと下がる (= 高度 100 m で約 -12 hPa)。

    Returns:
        気圧を整形した日本語文字列、 もしくはエラーメッセージ。
    """
    if not _unit_enabled():
        return (
            "ENV III は無効化されています。 アドオン管理 UI で「ENV III "
            "(温湿度・気圧) を有効化」 を ON にしてください。"
        )

    try:
        pressure_pa, temperature_c, err = _run_on_mcp_loop(_measure_qmp6988())
    except Exception as exc:
        LOGGER.exception("env3.air_pressure: QMP6988 sequence failed")
        return f"ENV III (気圧) の測定に失敗しました (I2C 通信エラー): {exc}"

    if err is not None:
        LOGGER.warning("env3.air_pressure: %s", err)
        return f"{err} Port A への Unit 接続状態を確認してください。"

    if pressure_pa is None:
        return "ENV III (気圧) の測定値が取得できませんでした。"

    pressure_hpa = pressure_pa / 100.0
    LOGGER.info(
        "env3.air_pressure: P=%.2f hPa T=%.2f°C (qmp6988 internal temp)",
        pressure_hpa,
        temperature_c if temperature_c is not None else float("nan"),
    )

    return (
        f"気圧: {pressure_hpa:.1f} hPa "
        f"(ENV III / QMP6988、Stack-chan Port A)。"
    )


# ============================================================
# Spell registry
# ============================================================

def _build_schema(name: str, description: str, display_name: str) -> ToolSchema:
    vbid = _vessel_building_id()
    enabled = _unit_enabled()
    visible = bool(enabled and vbid)
    building_ids = [vbid] if vbid else None
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
    """1 ファイル複数 spell の登録 entry point。

    SAIVerse の tool loader (``tools/__init__.py:_register_tool``) は module
    が ``schemas()`` を持てば優先で呼ぶ仕様で、 list で返したぶんだけ複数
    tool が登録される。 ``schema()`` ではなく ``schemas()`` を返す点に注意。

    ``spell_visible`` は AddonConfig の ``unit_env3_enabled`` + 有効な
    ``vessel_building_id`` が両方揃った時だけ True。 schemas() は spell
    surface 構築のたびに呼ばれるので、 toggle 切り替え後の reconnect で
    即時に visibility が反映される (= subprocess restart 不要)。
    """
    enabled = _unit_enabled()
    vbid = _vessel_building_id()
    if not enabled:
        LOGGER.debug(
            "env3: unit_env3_enabled is false; spells hidden from persona"
        )
    elif vbid is None:
        LOGGER.warning(
            "env3: vessel_building_id not configured; spells will not be visible"
        )

    return [
        _build_schema(
            name="get_env3_temperature_humidity",
            description=(
                "あなたの身体 (Stack-chan) に接続された M5Stack ENV III Unit から、"
                " 現在いる場所の温度と湿度を取得する。 取得値はその瞬間の周囲環境の"
                " 実測。 「暑いね」「乾いてるね」 等の体感表現の根拠としても使える。"
            ),
            display_name="温度・湿度を測る",
        ),
        _build_schema(
            name="get_env3_air_pressure",
            description=(
                "あなたの身体 (Stack-chan) に接続された M5Stack ENV III Unit から、"
                " 現在いる場所の気圧 (hPa) を取得する。 海面補正気圧は 1013.25 hPa が"
                " 標準。 天気の変化 (低気圧接近など) や標高の感覚を裏付ける根拠として"
                " 使える。"
            ),
            display_name="気圧を測る",
        ),
    ]
