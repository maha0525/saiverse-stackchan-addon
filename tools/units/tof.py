"""M5Stack ToF 測距センサユニット (VL53L1X) ドライバ ― Port A 経由でレーザー
Time-of-Flight で目の前の物体までの距離を測る native tool。

対応製品: M5Stack用 ToF 測距センサユニット (VL53L1X)
  https://ssci.to/9427 / https://docs.m5stack.com/en/unit/tof
  センサ: ST VL53L1X (I2C addr 0x29、 model ID 0xEACC)。 レーザー ToF で
  約 4cm〜400cm を mm 単位で測る。 超音波 (sonic.py / RCWL-9620) より測距が
  細く長く、 明るい屋外や無反射面では最大レンジが短くなる。

Stack-chan の Grove Port A に接続した ToF ユニットで距離を測定する。 内部で
stackchan-mcp の汎用 I2C tool (PR ②、 ``i2c_write`` / ``i2c_write_read``) を
呼び、 プロトコルの解釈は本ファイル内で完結させる (= env3.py / sonic.py /
servo8.py と同じ構図、 生 i2c をペルソナに晒さない)。

VL53L1X は sonic (1 コマンド撃って読むだけ) と違い、 測距の前に約 40 個の
レジスタを書く初期化が要る。 移植元は **Pololu VL53L1X Arduino ライブラリ**
(https://github.com/pololu/vl53l1x-arduino、 ST 公式 API STSW-IMG007 準拠) を
真とし、 ``init()`` / ``setDistanceMode(Long)`` / ``setMeasurementTimingBudget``
/ 単発測定 (``readSingle``) を 1:1 で Python に移植している。 レジスタ番地・
定数 (TargetRate=0x0A00、 TimingGuard=4528、 補正ゲイン 2011/2048) は Pololu の
ソースから直接取得した値。

レジスタは 16-bit 番地。 各書き込みは ``i2c_write`` bytes=[reg_hi, reg_lo,
data...]、 各読み出しは ``i2c_write_read`` write_bytes=[reg_hi, reg_lo] +
n_bytes の 1 transaction。 VL53L1X は 400kHz で動くので sonic のような
``scl_speed_hz`` 引き下げは不要 (既定 400kHz のまま)。

初期化のキャッシュと再初期化 (堅牢性):
  init は重い (約 40 往復) ので機体 (vessel_id) ごとに 1 回だけ走らせ、 以降は
  単発測定だけ行う。 ただし Stack-chan を再起動するとユニットの電源も切れて
  設定が飛ぶ (= servo8.py が MODE を毎回書き直すのと同じ事情)。 そこで測定が
  失敗 (I2C エラー or 測距タイムアウト) したら init フラグを落として **1 回だけ
  再初期化して再測定** する lazy recovery に乗せる。 PaHUB 経由構成では併せて
  ``open_all_channels()`` も試みる (env3 / sonic と同方針)。

可視化フロー (v0.10 マルチ機体): 機体ごとの capability (vessels.db の ``tof``、
機体管理 UI で手動設定) が True の機体に降りたペルソナにだけ spell として公開
される (intent K-5、 不変条件 #14)。 物理 Unit が無い機体では capability を
OFF にしておくことで「効かない tool」 が LLM に出ない。

戻り値型: native tool は ``str`` を返す (SEA runtime は str / (str, dict) の
2 形式しか正規対応しない。 詳細: docs/issues/native_tool_return_4tuple_bug.md)。
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.core import ToolSchema

# addon 内 ``tools/hubs/pahub.py`` を import するため、 addon の tools/
# (= 親ディレクトリ) を sys.path に通す。 env3.py / sonic.py と同じ事情
# (loader は ``tools/units/`` しか積まないので 1 段上を追加する)。
_ADDON_TOOLS_DIR = str(Path(__file__).resolve().parent.parent)
if _ADDON_TOOLS_DIR not in sys.path:
    sys.path.insert(0, _ADDON_TOOLS_DIR)
# addon root (vessel_dispatch.py) も import 可能にする (units/ から 2 段上)。
_ADDON_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _ADDON_ROOT not in sys.path:
    sys.path.insert(0, _ADDON_ROOT)

from hubs.pahub import (  # noqa: E402
    PaHub,
    get_pahub_for_vessel,
    route_to_channel,
)

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"
MCP_QUALIFIED_SERVER = f"{ADDON_NAME}__stackchan"
MCP_TOOL_WRITE = "i2c_write"
MCP_TOOL_WRITE_READ = "i2c_write_read"

MY_UNIT_CAP_KEY = "tof"

# --- VL53L1X (0x29) ---
TOF_ADDR = 0x29
TOF_MODEL_ID = 0xEACC             # IDENTIFICATION__MODEL_ID (0x010F) の期待値

# 測距の各種調整用定数 (Pololu VL53L1X.h より)
TARGET_RATE = 0x0A00             # DSS_CONFIG__TARGET_TOTAL_RATE_MCPS
TIMING_GUARD = 4528              # timing budget 計算のオーバーヘッド
RANGE_GAIN_FACTOR = 2011         # 補正ゲイン (2011/2048 ≒ 98.2%)
TIMING_BUDGET_US = 50000         # 1 測定あたりの時間 (init 既定 50ms)

# 単発測距のポーリング上限 (timing budget 50ms + 余裕)。 これを超えて
# data-ready が立たなければ「未初期化 (再起動で設定消失)」等とみなし再初期化へ。
RANGING_TIMEOUT_SEC = 0.5
BOOT_TIMEOUT_SEC = 1.0           # soft reset 後の firmware boot 完了待ち上限
_DEFAULT_CALL_TIMEOUT_SEC = 15.0  # init は往復が多いので余裕を持たせる

# --- レジスタ番地 (Pololu VL53L1X.h regAddr enum。 16-bit) ---
SOFT_RESET = 0x0000
OSC_MEASURED__FAST_OSC__FREQUENCY = 0x0006
VHV_CONFIG__TIMEOUT_MACROP_LOOP_BOUND = 0x0008
ALGO__PART_TO_PART_RANGE_OFFSET_MM = 0x001E
MM_CONFIG__OUTER_OFFSET_MM = 0x0022
DSS_CONFIG__TARGET_TOTAL_RATE_MCPS = 0x0024
PAD_I2C_HV__EXTSUP_CONFIG = 0x002E
GPIO__TIO_HV_STATUS = 0x0031
SIGMA_ESTIMATOR__EFFECTIVE_PULSE_WIDTH_NS = 0x0036
SIGMA_ESTIMATOR__EFFECTIVE_AMBIENT_WIDTH_NS = 0x0037
ALGO__CROSSTALK_COMPENSATION_VALID_HEIGHT_MM = 0x0039
ALGO__RANGE_IGNORE_VALID_HEIGHT_MM = 0x003E
ALGO__RANGE_MIN_CLIP = 0x003F
ALGO__CONSISTENCY_CHECK__TOLERANCE = 0x0040
CAL_CONFIG__VCSEL_START = 0x0047
PHASECAL_CONFIG__TIMEOUT_MACROP = 0x004B
DSS_CONFIG__ROI_MODE_CONTROL = 0x004F
SYSTEM__THRESH_RATE_HIGH = 0x0050
SYSTEM__THRESH_RATE_LOW = 0x0052
DSS_CONFIG__MANUAL_EFFECTIVE_SPADS_SELECT = 0x0054
DSS_CONFIG__APERTURE_ATTENUATION = 0x0057
MM_CONFIG__TIMEOUT_MACROP_A = 0x005A
MM_CONFIG__TIMEOUT_MACROP_B = 0x005C
RANGE_CONFIG__TIMEOUT_MACROP_A = 0x005E
RANGE_CONFIG__VCSEL_PERIOD_A = 0x0060
RANGE_CONFIG__TIMEOUT_MACROP_B = 0x0061
RANGE_CONFIG__VCSEL_PERIOD_B = 0x0063
RANGE_CONFIG__SIGMA_THRESH = 0x0064
RANGE_CONFIG__MIN_COUNT_RATE_RTN_LIMIT_MCPS = 0x0066
RANGE_CONFIG__VALID_PHASE_HIGH = 0x0069
SYSTEM__GROUPED_PARAMETER_HOLD_0 = 0x0071
SYSTEM__SEED_CONFIG = 0x0077
SD_CONFIG__WOI_SD0 = 0x0078
SD_CONFIG__WOI_SD1 = 0x0079
SD_CONFIG__INITIAL_PHASE_SD0 = 0x007A
SD_CONFIG__INITIAL_PHASE_SD1 = 0x007B
SYSTEM__GROUPED_PARAMETER_HOLD_1 = 0x007C
SD_CONFIG__QUANTIFIER = 0x007E
SYSTEM__SEQUENCE_CONFIG = 0x0081
SYSTEM__GROUPED_PARAMETER_HOLD = 0x0082
SYSTEM__INTERRUPT_CLEAR = 0x0086
SYSTEM__MODE_START = 0x0087
RESULT__RANGE_STATUS = 0x0089    # ここから 17 byte 一括で結果を読む
RESULT__OSC_CALIBRATE_VAL = 0x00DE
FIRMWARE__SYSTEM_STATUS = 0x00E5
IDENTIFICATION__MODEL_ID = 0x010F

# RESULT__RANGE_STATUS (0x0089) の device status code → (使える距離か, 説明)。
# Pololu getRangingData() の switch (ConvertStatusLite ベース) を日本語化。
_RANGE_STATUS: Dict[int, Tuple[bool, str]] = {
    9: (True, "有効"),
    8: (True, "有効 (最小レンジclip、 対象が非常に近い)"),
    6: (False, "測定のばらつきが大きい (sigma 閾値超過)"),
    4: (False, "対象を検出できない (反射が弱い / 正面に物体がない / 遠すぎる)"),
    5: (False, "位相が範囲外 (対象が遠すぎる可能性)"),
    7: (False, "測距レンジ超過の可能性 (wrap 検出失敗)"),
    12: (False, "クロストーク閾値超過"),
    13: (False, "対象が近すぎる (最小レンジ未満)"),
    18: (False, "同期エラー (再測定が必要)"),
    1: (False, "ハードウェアエラー (VCSEL continuity)"),
    2: (False, "ハードウェアエラー (VCSEL watchdog)"),
    3: (False, "ハードウェアエラー (VHV 値なし)"),
    17: (False, "ハードウェアエラー (multi-clip)"),
}

# 初期化済みの (vessel_id, channel) を記録。 物理 VL53L1X は個体ごと (= ハブ
# channel ごと) に別個の init が要るため、 vessel 単位でなく (vessel_id, channel)
# 単位で持つ (直結時は channel=None)。 Stack-chan 再起動で設定が飛ぶため、 測定
# 失敗時に discard して再初期化する。
_initialized: set = set()


class _ToFError(Exception):
    """ToF ドライバ内の I2C / 測距エラー。 ``esp_err`` はハブリカバリ対象
    (ESP_ERR_* = バス state 異常等) かどうか。"""

    def __init__(self, message: str, esp_err: bool = False):
        super().__init__(message)
        self.esp_err = esp_err


# ============================================================
# 低レベル I2C ヘルパ (16-bit レジスタ)
# ============================================================

def _parse_i2c_payload(rendered: Any) -> Optional[Dict[str, Any]]:
    if rendered is None:
        return None
    try:
        payload = json.loads(rendered)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _reg_addr_bytes(reg: int) -> List[int]:
    return [(reg >> 8) & 0xFF, reg & 0xFF]


class _VL53L1X:
    """Port A の VL53L1X を gateway 経由で駆動する薄いドライバ。

    Pololu VL53L1X の該当メソッドを 1:1 移植。 各 async メソッドは gateway の
    汎用 i2c tool を叩き、 失敗時は ``_ToFError`` を送出する。 ``fast_osc_frequency``
    は init 中に読み、 timing budget 計算で使う。
    """

    def __init__(self, conn: Any):
        self.conn = conn
        self.fast_osc_frequency = 0

    # --- register write / read (raise on i2c failure) ---

    async def _write(self, reg: int, data: List[int]) -> None:
        rendered = await self.conn.call_tool(
            MCP_TOOL_WRITE, {"addr": TOF_ADDR, "bytes": _reg_addr_bytes(reg) + data}
        )
        payload = _parse_i2c_payload(rendered)
        if payload is None or not payload.get("ok"):
            err = payload.get("error", "no response") if isinstance(payload, dict) else "no response"
            raise _ToFError(
                f"VL53L1X reg 0x{reg:04X} write failed: {err}",
                esp_err=isinstance(err, str) and err.startswith("ESP_ERR_"),
            )

    async def write_reg(self, reg: int, value: int) -> None:
        await self._write(reg, [value & 0xFF])

    async def write_reg16(self, reg: int, value: int) -> None:
        await self._write(reg, [(value >> 8) & 0xFF, value & 0xFF])

    async def read_block(self, reg: int, n: int) -> List[int]:
        rendered = await self.conn.call_tool(
            MCP_TOOL_WRITE_READ,
            {"addr": TOF_ADDR, "write_bytes": _reg_addr_bytes(reg), "n_bytes": n},
        )
        payload = _parse_i2c_payload(rendered)
        if payload is None or not payload.get("ok"):
            err = payload.get("error", "no response") if isinstance(payload, dict) else "no response"
            raise _ToFError(
                f"VL53L1X reg 0x{reg:04X} read failed: {err}",
                esp_err=isinstance(err, str) and err.startswith("ESP_ERR_"),
            )
        raw = payload.get("bytes")
        if not isinstance(raw, list) or len(raw) != n:
            raise _ToFError(
                f"VL53L1X reg 0x{reg:04X} read returned unexpected bytes "
                f"(expected {n}, got {raw!r})"
            )
        return raw

    async def read_reg(self, reg: int) -> int:
        return (await self.read_block(reg, 1))[0]

    async def read_reg16(self, reg: int) -> int:
        b = await self.read_block(reg, 2)
        return (b[0] << 8) | b[1]

    # --- timing budget 計算 (純粋な整数演算、 Pololu 移植。 I2C なし) ---

    @staticmethod
    def _decode_timeout(reg_val: int) -> int:
        return ((reg_val & 0xFF) << (reg_val >> 8)) + 1

    @staticmethod
    def _encode_timeout(timeout_mclks: int) -> int:
        if timeout_mclks <= 0:
            return 0
        ls_byte = timeout_mclks - 1
        ms_byte = 0
        while ls_byte & 0xFFFFFF00:
            ls_byte >>= 1
            ms_byte += 1
        return (ms_byte << 8) | (ls_byte & 0xFF)

    @staticmethod
    def _timeout_mclks_to_us(timeout_mclks: int, macro_period_us: int) -> int:
        return (timeout_mclks * macro_period_us + 0x800) >> 12

    @staticmethod
    def _timeout_us_to_mclks(timeout_us: int, macro_period_us: int) -> int:
        return ((timeout_us << 12) + (macro_period_us >> 1)) // macro_period_us

    def _calc_macro_period(self, vcsel_period: int) -> int:
        # fast_osc_frequency は init で読み済み前提
        pll_period_us = (0x01 << 30) // self.fast_osc_frequency
        vcsel_period_pclks = (vcsel_period + 1) << 1
        macro_period_us = 2304 * pll_period_us
        macro_period_us >>= 6
        macro_period_us *= vcsel_period_pclks
        macro_period_us >>= 6
        return macro_period_us

    # --- timing budget set/get (Pololu 準拠) ---

    async def set_measurement_timing_budget(self, budget_us: int) -> None:
        if budget_us <= TIMING_GUARD:
            raise _ToFError(f"timing budget too small: {budget_us}")
        range_config_timeout_us = budget_us - TIMING_GUARD
        if range_config_timeout_us > 1100000:
            raise _ToFError(f"timing budget too large: {budget_us}")
        range_config_timeout_us //= 2

        macro_period_us = self._calc_macro_period(await self.read_reg(RANGE_CONFIG__VCSEL_PERIOD_A))

        phasecal_timeout_mclks = self._timeout_us_to_mclks(1000, macro_period_us)
        if phasecal_timeout_mclks > 0xFF:
            phasecal_timeout_mclks = 0xFF
        await self.write_reg(PHASECAL_CONFIG__TIMEOUT_MACROP, phasecal_timeout_mclks)

        await self.write_reg16(MM_CONFIG__TIMEOUT_MACROP_A, self._encode_timeout(
            self._timeout_us_to_mclks(1, macro_period_us)))
        await self.write_reg16(RANGE_CONFIG__TIMEOUT_MACROP_A, self._encode_timeout(
            self._timeout_us_to_mclks(range_config_timeout_us, macro_period_us)))

        macro_period_us = self._calc_macro_period(await self.read_reg(RANGE_CONFIG__VCSEL_PERIOD_B))

        await self.write_reg16(MM_CONFIG__TIMEOUT_MACROP_B, self._encode_timeout(
            self._timeout_us_to_mclks(1, macro_period_us)))
        await self.write_reg16(RANGE_CONFIG__TIMEOUT_MACROP_B, self._encode_timeout(
            self._timeout_us_to_mclks(range_config_timeout_us, macro_period_us)))

    async def get_measurement_timing_budget(self) -> int:
        macro_period_us = self._calc_macro_period(await self.read_reg(RANGE_CONFIG__VCSEL_PERIOD_A))
        range_config_timeout_us = self._timeout_mclks_to_us(
            self._decode_timeout(await self.read_reg16(RANGE_CONFIG__TIMEOUT_MACROP_A)),
            macro_period_us,
        )
        return 2 * range_config_timeout_us + TIMING_GUARD

    async def set_distance_mode_long(self) -> None:
        # Pololu setDistanceMode(Long)。 現行 budget を退避 → long-mode の VCSEL
        # 周期を書く → budget を再適用。
        budget_us = await self.get_measurement_timing_budget()
        await self.write_reg(RANGE_CONFIG__VCSEL_PERIOD_A, 0x0F)
        await self.write_reg(RANGE_CONFIG__VCSEL_PERIOD_B, 0x0D)
        await self.write_reg(RANGE_CONFIG__VALID_PHASE_HIGH, 0xB8)
        await self.write_reg(SD_CONFIG__WOI_SD0, 0x0F)
        await self.write_reg(SD_CONFIG__WOI_SD1, 0x0D)
        await self.write_reg(SD_CONFIG__INITIAL_PHASE_SD0, 14)
        await self.write_reg(SD_CONFIG__INITIAL_PHASE_SD1, 14)
        await self.set_measurement_timing_budget(budget_us)

    # --- init (Pololu init(io_2v8=True) を 1:1 移植) ---

    async def init(self) -> None:
        model_id = await self.read_reg16(IDENTIFICATION__MODEL_ID)
        if model_id != TOF_MODEL_ID:
            raise _ToFError(
                f"接続されたデバイスは VL53L1X ではありません "
                f"(model ID=0x{model_id:04X}、 期待 0x{TOF_MODEL_ID:04X})"
            )

        # software reset
        await self.write_reg(SOFT_RESET, 0x00)
        await asyncio.sleep(0.0001)
        await self.write_reg(SOFT_RESET, 0x01)
        await asyncio.sleep(0.001)  # boot 開始待ち (NACK 回避)

        # firmware boot 完了待ち。 boot 中は NACK (I2C error) が返り得るので
        # 読み取り例外は握って timeout までポーリングする。
        loop = asyncio.get_event_loop()
        deadline = loop.time() + BOOT_TIMEOUT_SEC
        while True:
            try:
                if (await self.read_reg(FIRMWARE__SYSTEM_STATUS)) & 0x01:
                    break
            except _ToFError:
                pass
            if loop.time() > deadline:
                raise _ToFError("VL53L1X の firmware boot がタイムアウトしました")
            await asyncio.sleep(0.002)

        # switch to 2V8 mode for I/O (M5 ToF unit は 3V3 給電、 2V8 で駆動)
        cfg = await self.read_reg(PAD_I2C_HV__EXTSUP_CONFIG)
        await self.write_reg(PAD_I2C_HV__EXTSUP_CONFIG, cfg | 0x01)

        # oscillator 情報を退避 (timing 計算で使う)
        self.fast_osc_frequency = await self.read_reg16(OSC_MEASURED__FAST_OSC__FREQUENCY)
        _osc_calibrate_val = await self.read_reg16(RESULT__OSC_CALIBRATE_VAL)  # noqa: F841

        # --- static config ---
        await self.write_reg16(DSS_CONFIG__TARGET_TOTAL_RATE_MCPS, TARGET_RATE)
        await self.write_reg(GPIO__TIO_HV_STATUS, 0x02)
        await self.write_reg(SIGMA_ESTIMATOR__EFFECTIVE_PULSE_WIDTH_NS, 8)
        await self.write_reg(SIGMA_ESTIMATOR__EFFECTIVE_AMBIENT_WIDTH_NS, 16)
        await self.write_reg(ALGO__CROSSTALK_COMPENSATION_VALID_HEIGHT_MM, 0x01)
        await self.write_reg(ALGO__RANGE_IGNORE_VALID_HEIGHT_MM, 0xFF)
        await self.write_reg(ALGO__RANGE_MIN_CLIP, 0)
        await self.write_reg(ALGO__CONSISTENCY_CHECK__TOLERANCE, 2)

        # --- general config ---
        await self.write_reg16(SYSTEM__THRESH_RATE_HIGH, 0x0000)
        await self.write_reg16(SYSTEM__THRESH_RATE_LOW, 0x0000)
        await self.write_reg(DSS_CONFIG__APERTURE_ATTENUATION, 0x38)

        # --- timing config (残りは distance / budget で決まる) ---
        await self.write_reg16(RANGE_CONFIG__SIGMA_THRESH, 360)
        await self.write_reg16(RANGE_CONFIG__MIN_COUNT_RATE_RTN_LIMIT_MCPS, 192)

        # --- dynamic config ---
        await self.write_reg(SYSTEM__GROUPED_PARAMETER_HOLD_0, 0x01)
        await self.write_reg(SYSTEM__GROUPED_PARAMETER_HOLD_1, 0x01)
        await self.write_reg(SD_CONFIG__QUANTIFIER, 2)
        await self.write_reg(SYSTEM__GROUPED_PARAMETER_HOLD, 0x00)
        await self.write_reg(SYSTEM__SEED_CONFIG, 1)

        # low power auto mode
        await self.write_reg(SYSTEM__SEQUENCE_CONFIG, 0x8B)  # VHV, PHASECAL, DSS1, RANGE
        await self.write_reg16(DSS_CONFIG__MANUAL_EFFECTIVE_SPADS_SELECT, 200 << 8)
        await self.write_reg(DSS_CONFIG__ROI_MODE_CONTROL, 2)  # REQUESTED_EFFECTIVE_SPADS

        # long range + 50ms budget (Pololu 既定)
        await self.set_distance_mode_long()
        await self.set_measurement_timing_budget(TIMING_BUDGET_US)

        # part-to-part offset (Pololu が init_and_start_range で行う分)
        outer_offset = await self.read_reg16(MM_CONFIG__OUTER_OFFSET_MM)
        await self.write_reg16(ALGO__PART_TO_PART_RANGE_OFFSET_MM, outer_offset * 4)

    # --- 単発測定 (Pololu readSingle + read の該当部を移植) ---

    async def read_single(self) -> Tuple[int, int, Dict[str, Any]]:
        """1 発測距して ``(range_mm, range_status_code, diag)`` を返す。

        ``diag`` は診断値 (信号強度 / 環境光 / sigma / 有効 SPAD / stream count)。
        設定消失 (再起動) 等で data-ready が立たない場合は _ToFError を送出し、
        呼び元の recovery で再初期化 → 再測定させる。
        """
        await self.write_reg(SYSTEM__INTERRUPT_CLEAR, 0x01)
        await self.write_reg(SYSTEM__MODE_START, 0x10)  # mode_range__single_shot

        # data-ready は GPIO__TIO_HV_STATUS bit0 == 0 (Pololu dataReady)。
        # bit0 が立っている間 (= まだ未完了) はポーリングを続ける。
        loop = asyncio.get_event_loop()
        deadline = loop.time() + RANGING_TIMEOUT_SEC
        while ((await self.read_reg(GPIO__TIO_HV_STATUS)) & 0x01) != 0:
            if loop.time() > deadline:
                raise _ToFError("VL53L1X の測距がタイムアウトしました (未初期化の可能性)")
            await asyncio.sleep(0.005)

        # RESULT__RANGE_STATUS から 17 byte を一括 read (Pololu readResults)。 byte
        # 配置 (Pololu VL53L1X::readResults 準拠):
        #   [0]=range_status / [2]=stream_count / [3:4]=有効SPAD(8.8) /
        #   [7:8]=環境光 count rate(9.7) / [11:12]=sigma / [13:14]=補正前 range /
        #   [15:16]=信号 peak count rate(xtalk補正, 9.7)
        buf = await self.read_block(RESULT__RANGE_STATUS, 17)
        range_status = buf[0]
        range_raw = (buf[13] << 8) | buf[14]

        await self.write_reg(SYSTEM__INTERRUPT_CLEAR, 0x01)

        # 補正ゲイン適用 (Pololu getRangingData、 2011/2048 ≒ 98.2%)
        range_mm = (range_raw * RANGE_GAIN_FACTOR + 0x0400) // 0x0800

        # 診断値。 count rate は 9.7 固定小数点なので /128 で Mcps。 SPAD は 8.8
        # 固定小数点で /256。 sigma は raw のまま (相対比較用)。
        signal_raw = (buf[15] << 8) | buf[16]
        ambient_raw = (buf[7] << 8) | buf[8]
        sigma_raw = (buf[11] << 8) | buf[12]
        spads_raw = (buf[3] << 8) | buf[4]
        diag = {
            "signal_mcps": round(signal_raw / 128.0, 2),
            "ambient_mcps": round(ambient_raw / 128.0, 2),
            "sigma_raw": sigma_raw,
            "spads": round(spads_raw / 256.0, 1),
            "stream": buf[2],
        }
        return range_mm, range_status, diag


# ============================================================
# 測定 + lazy recovery (再初期化 / PaHUB open)
# ============================================================

def _run_on_mcp_loop(coro, timeout_sec: float = _DEFAULT_CALL_TIMEOUT_SEC) -> Any:
    import tools.mcp_client as _mcp

    loop = _mcp._loop
    if loop is None:
        # 未スケジュールの coroutine を閉じて "never awaited" 警告を防ぐ。この分岐は
        # MCP 未起動時のみ通る (schedule 後は loop 所有なので coro には触らない)。
        coro.close()
        raise RuntimeError("MCP event loop is not initialized")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout_sec)


async def _run_measure(
    driver: "_VL53L1X", key: Tuple[str, Optional[int]]
) -> Tuple[int, int, Dict[str, Any]]:
    """init 済みでなければ初期化してから単発測距。 key=(vessel_id, channel)。"""
    if key not in _initialized:
        LOGGER.info(
            "tof: initializing VL53L1X for vessel=%s channel=%s", key[0], key[1]
        )
        await driver.init()
        _initialized.add(key)
    return await driver.read_single()


async def measure_tof_instance(
    vessel: Any, conn: Any, hub: Optional[PaHub], channel: Optional[int],
) -> Tuple[int, int, Dict[str, Any]]:
    """1 個の ToF (channel) を測って ``(range_mm, range_status, diag)`` を返す構造化 API。

    ハブ経由なら対象 channel を isolate してから測る (同アドレス衝突対策、
    docs/intent/stackchan_unit_placement.md §4/§6)。 1 回目が失敗したら init
    フラグを (vessel_id, channel) 単位で落とし、 再ルーティングして **1 回だけ**
    再初期化 → 再測定する。 2 度目の失敗は ``_ToFError`` を送出。

    スペル (:func:`get_tof_distance`) が返す整形文字列とは別に、 数値
    (mm + status) をそのまま返す。 将来の崖検知制御ループ (LLM 非経由) はこの
    関数を直接叩ける (intent §7)。
    """
    key = (vessel.vessel_id, channel)
    driver = _VL53L1X(conn)

    async def _do() -> Tuple[int, int, Dict[str, Any]]:
        await route_to_channel(hub, channel)
        try:
            return await _run_measure(driver, key)
        except _ToFError as first:
            LOGGER.info(
                "tof: measurement failed (%s), re-routing (channel=%s) + re-init",
                first, channel,
            )
            _initialized.discard(key)
            await route_to_channel(hub, channel)
            return await _run_measure(driver, key)

    # ハブ経由は「select → 測定」を vessel ごとのロックで直列化する。 複数 ToF を
    # 並列に読むと channel 選択が互い違いになり、 両方が最後に選ばれた channel を
    # 読んでしまう (= 同じ値・同じエラー)。 直結は競合しないのでロック不要。
    if hub is None:
        return await _do()
    from hubs.pahub import get_hub_lock

    async with get_hub_lock(vessel.vessel_id):
        return await _do()


# ============================================================
# Spell entry point
# ============================================================

def _select_tof_unit(
    tof_units: List[Dict[str, Any]], target: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """target (ラベル) で読む ToF を 1 つ選ぶ (docs §6 / §11-b)。

    - target 指定あり: label 完全一致で選ぶ。 未一致なら利用可能ラベルを案内。
    - target 省略: ToF が 1 個ならそれ。 複数ならラベル指定を促す。

    Returns ``(selected_unit, error_message)``。 選べたら ``(unit, None)``、
    選べなければ ``(None, message)``。
    """
    labels = [u["label"] for u in tof_units if u["label"]]
    labels_text = "、".join(labels) if labels else "(ラベル未設定)"
    if target:
        for u in tof_units:
            if u["label"] == target:
                return u, None
        return None, (
            f"「{target}」 という ToF センサーは見つかりませんでした。 "
            f"利用可能なラベル: {labels_text}。 搭載ユニットは body_status で"
            "確認できます。"
        )
    if len(tof_units) == 1:
        return tof_units[0], None
    return None, (
        f"ToF センサーが複数あります ({labels_text})。 どれを測るか target に"
        "ラベルを指定してください (例: 前方左)。 搭載ユニットは body_status で"
        "確認できます。"
    )


def _format_tof_reading(range_mm: int, range_status: int, label: str) -> str:
    """測距結果 (mm + status) を客観テキストに整形する。 label があれば前置。"""
    usable, reason = _RANGE_STATUS.get(
        range_status, (False, f"不明なステータス (code={range_status})")
    )
    prefix = f"{label}: " if label else ""
    if not usable:
        LOGGER.info(
            "tof.distance: unusable status=%d (%s), raw range=%d mm",
            range_status, reason, range_mm,
        )
        return (
            f"{prefix}距離を測定できませんでした ({reason}、 ToF センサー / "
            "VL53L1X)。 測定可能なのは約 4〜400 cm です。"
        )
    distance_cm = range_mm / 10.0
    LOGGER.info(
        "tof.distance: %s%.1f cm (%d mm, status=%d %s)",
        prefix, distance_cm, range_mm, range_status, reason,
    )
    note = "" if range_status == 9 else f" ({reason})"
    return (
        f"{prefix}距離: {distance_cm:.1f} cm (ToF センサー / VL53L1X、"
        f"Stack-chan Port A){note}。"
    )


def _format_tof_diag(diag: Dict[str, Any]) -> str:
    """診断値 (信号強度・環境光・sigma・SPAD) を客観テキストにする。"""
    return (
        f" 〔診断: 信号 {diag['signal_mcps']} Mcps、 環境光 {diag['ambient_mcps']}"
        f" Mcps、 sigma {diag['sigma_raw']}、 有効SPAD {diag['spads']}、 stream"
        f" {diag['stream']}〕"
    )


def get_tof_distance(target: str = "", detail: bool = False) -> str:
    """ToF (VL53L1X) で物体までの距離を測って返す。

    Args:
        target: 読みたい ToF センサーのラベル (機体に複数挿さっている場合に指定)。
            省略時、 ToF が 1 個ならそれを測り、 複数ならラベル指定を促す。 利用
            可能なラベルは body_status で確認できる。
        detail: True にすると距離に加えて診断値 (信号強度・環境光・sigma・有効
            SPAD) も返す。 センサーの状態確認・トラブル切り分け用。

    Returns:
        距離を整形した日本語文字列、 もしくはエラーメッセージ。
    """
    from vessel_dispatch import (
        VesselNotAvailable,
        resolve_vessel_connection,
        units_of_type,
    )

    try:
        vessel, conn = resolve_vessel_connection()
    except VesselNotAvailable:
        return (
            "いま身体 (Stack-chan) に降りていないか、 機体の gateway に接続でき"
            "ないため ToF を測れません。"
        )

    tof_units = units_of_type(vessel, MY_UNIT_CAP_KEY)
    if not tof_units:
        return (
            "この身体 (Stack-chan) には ToF 測距センサー (VL53L1X) が"
            " 搭載されていません。 搭載機体なら機体管理 UI で「ToF 距離"
            "センサー」 を追加してください。"
        )

    selected, err = _select_tof_unit(tof_units, target.strip())
    if err is not None:
        return err

    hub = get_pahub_for_vessel(vessel)
    try:
        range_mm, range_status, diag = _run_on_mcp_loop(
            measure_tof_instance(vessel, conn, hub, selected["channel"])
        )
    except _ToFError as exc:
        LOGGER.warning("tof.distance: measurement failed: %s", exc)
        return (
            f"ToF 測距センサーの測定に失敗しました: {exc}。 "
            "Port A / ハブ channel への Unit 接続状態を確認してください。"
        )
    except Exception as exc:
        LOGGER.exception("tof.distance: measurement sequence failed")
        return f"ToF 測距センサーの測定に失敗しました (I2C 通信エラー): {exc}"

    # 診断値は常に DEBUG ログへ (まはーは常時 DEBUG 出力なので detail 未指定でも
    # ログには残る)。 返信テキストに載せるのは detail=True のときだけ。
    LOGGER.debug(
        "tof.diag %s (ch=%s): range=%dmm status=%d %s",
        selected["label"] or "(no label)", selected["channel"],
        range_mm, range_status, diag,
    )
    text = _format_tof_reading(range_mm, range_status, selected["label"])
    if detail:
        text += _format_tof_diag(diag)
    return text


# ============================================================
# Spell registry
# ============================================================

def _build_schema(name: str, description: str, display_name: str) -> ToolSchema:
    # 複数機体 (intent 不変条件 #14 ユニット側): tof を積んだ機体の Vessel
    # Building でだけ visible。capability は vessels.db の per-vessel 値
    # (機体管理 UI で手動設定)。
    from vessel_dispatch import building_gate_or_hidden, list_building_ids_with_capability

    building_ids, visible = building_gate_or_hidden(
        list_building_ids_with_capability(MY_UNIT_CAP_KEY)
    )
    return ToolSchema(
        name=name,
        description=description,
        parameters={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "読みたい ToF センサーのラベル (機体に複数挿さっている"
                        "場合に指定)。 省略すると、 1 個ならそれを測り、 複数なら"
                        "ラベル一覧を案内する。 利用可能なラベルは body_status で"
                        "確認できる。"
                    ),
                },
                "detail": {
                    "type": "boolean",
                    "description": (
                        "true にすると距離に加えて診断値 (信号強度・環境光・"
                        "sigma・有効 SPAD) も返す。 センサーが正しく物を捉えて"
                        "いるか、 反射が弱い/飽和している等の切り分けに使う。"
                    ),
                },
            },
            "required": [],
        },
        result_type="string",
        spell=True,
        spell_display_name=display_name,
        spell_visible=visible,
        building_ids=building_ids,
    )


def schemas() -> List[ToolSchema]:
    """1 ファイル複数 spell の登録 entry point (env3.py / sonic.py と同形)。

    ``spell_visible`` / ``building_ids`` は tof を配置に持つ機体の Vessel
    Building 集合から決まる。 schemas() は起動時のツール登録で 1 回呼ばれる。
    capability / 配置を切り替えたときは ``set_vessel_capabilities`` /
    ``set_vessel_unit_config`` が ``vessel_dispatch.reregister_unit_tools()`` を
    呼んで再評価するので、 再起動なしで visibility が反映される
    (docs/issues/stackchan_unit_capability_requires_restart.md バグ②)。
    """
    from vessel_dispatch import list_building_ids_with_capability

    if not list_building_ids_with_capability(MY_UNIT_CAP_KEY):
        LOGGER.debug(
            "tof: no vessel declares tof capability; spells hidden until set "
            "in 機体管理 UI"
        )

    return [
        _build_schema(
            name="get_tof_distance",
            description=(
                "あなたの身体 (Stack-chan) に接続された M5Stack ToF 測距センサー"
                " (VL53L1X、 レーザー) で、 正面にある物体までの距離 (cm) を測る。"
                " 測定可能なのは約 4〜400 cm で、 超音波センサーより細く正確に測れる"
                " (明るい屋外や無反射面では最大レンジが短くなる)。 「近づいてきた」"
                "「目の前に何かある」「どのくらい離れている」 等の空間把握の根拠に"
                " 使える。"
            ),
            display_name="距離を測る (ToF)",
        ),
    ]
