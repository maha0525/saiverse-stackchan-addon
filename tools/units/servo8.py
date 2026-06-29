"""M5Stack 8Servos Unit (U165) ドライバ ― Port A 経由でサーボを制御する。

Stack-chan の Grove Port A に接続した M5Stack 8Servos Unit (U165、
STM32F030F4、 I2C addr 0x25) の指定チャンネルを制御する native tool。 内部で
stackchan-mcp の汎用 I2C tool (PR ②、 ``i2c_write``) を呼び、 レジスタ
プロトコルの解釈は本ファイル内で完結させる (= env3.py と同じ構図、 生 i2c の
組み立てをペルソナ / アクション定義に晒さない)。

レジスタプロトコル (M5Stack 公式 Arduino ライブラリ ``m5stack/M5Unit-8Servo``
の ``M5_UNIT_8SERVO.h`` / ``.cpp`` を真として移植。 angle→pulse 変換は内部
ファーム ``m5stack/Unit8Servo-Internal-FW`` の ``Steer.c`` で裏取り):

  - MODE_REG  (0x00 + ch)        : チャンネルの I/O モード (1 byte)。
                                    サーボ駆動には SERVO_CTL_MODE (=3) を書く。
  - ANGLE_REG (0x50 + ch)        : サーボ角 0..180° (1 byte)。
  - PULSE_REG (0x60 + ch*2)      : サーボパルス幅 500..2500µs (2 byte LE)。
  - ※ 公式 lib の setServoAngle / setServoPulse は MODE を触らない。 つまり
    値を書く前に別途 MODE を SERVO_CTL_MODE にしておく必要がある (電源投入
    直後 / Stack-chan 再起動後の既定は DIGITAL_INPUT_MODE=0 で出力されない)。

  ファームの変換式 (``Steer.c``):
    ANGLE_TO_PULSE(angle) = 500 + angle * 2000 / 180
    → angle 0°=500µs, 90°=1500µs, 180°=2500µs

180° サーボ (位置決め) と 360° サーボ (連続回転) で操作の意味が変わる:
  - 180° サーボ: 角度がそのまま物理的な向き。 ``servo8_set_angle`` を使う。
  - 360° サーボ (車輪等): パルス幅が回転速度・方向を表す。 1500µs (=中立) で
    停止、 そこからの差が速度、 符号が方向。 ``servo8_set_speed`` を使う。
    回転は非停止値を保持している間ずっと続く (= ファームが PWM を出し続ける)。
    「○秒だけ前進」 のような時間制御は複合アクション (set_speed → wait →
    set_speed 0) で表現する (intent doc「前進」 パターン)。

モード管理方針: **毎回 MODE → 値の 2 段書き込み**を行う。 チャンネルが既に
サーボモードかをキャッシュする手もあるが、 Stack-chan 再起動でユニットのピンが
既定モードに戻ると「ACK は返るがサーボが動かない」 沈黙故障になる。 毎回 MODE
を assert すれば再起動を跨いでも堅牢で、 仮に MODE 再書き込みで PWM が一瞬
乱れる (= twitch) としても挙動が目に見えるので実機デバッグが速い。 実機で
twitch が観測されたらキャッシュ化 (= 初回のみ MODE 書き込み) に倒す。

有効化フロー: AddonConfig の ``unit_servo8_enabled`` を true にすると spell
として公開される (= addon UI の toggle で ON/OFF)。 物理 Unit が無い時は
無効化したままにすることで「効かない tool」 が LLM に出ないようにする。

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
# (= 親ディレクトリの親) を sys.path に通す。 env3.py と同じ事情
# (loader は ``tools/units/`` しか積まないので 1 段上を追加する)。
_ADDON_TOOLS_DIR = str(Path(__file__).resolve().parent.parent)
if _ADDON_TOOLS_DIR not in sys.path:
    sys.path.insert(0, _ADDON_TOOLS_DIR)

from hubs.pahub import PaHub, get_pahub_from_params  # noqa: E402

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"
MCP_QUALIFIED_SERVER = f"{ADDON_NAME}__stackchan"
MCP_TOOL_WRITE = "i2c_write"

# --- M5Stack 8Servos Unit (U165) レジスタ ---
SERVO8_ADDR = 0x25
MODE_REG = 0x00          # base; チャンネル ch のモードは MODE_REG + ch
ANGLE_REG = 0x50         # base; チャンネル ch の角は ANGLE_REG + ch
PULSE_REG = 0x60         # base; チャンネル ch のパルス幅は PULSE_REG + ch*2 (2byte LE)
SERVO_CTL_MODE = 0x03    # extio_io_mode_t::SERVO_CTL_MODE

NUM_CHANNELS = 8
ANGLE_MIN = 0
ANGLE_MAX = 180

# 連続回転サーボの速度→パルス幅マッピング。 1500µs を中立 (停止) とし、
# speed ±100 を ±500µs (= 1000〜2000µs) に対応させる。 これは連続回転サーボの
# 一般的なコマンド範囲 (full 500〜2500µs はユニット的には可能だが、 大半の
# 連続回転サーボは ±500µs で飽和するので過駆動を避けて保守的に取る)。 個体の
# 正確な中立 / デッドバンドは製品差があるため、 厳密な停止が要る場合は
# servo8_set_angle の代わりに実機で trim 確認が必要 (= 将来 neutral 設定を
# AddonConfig 化する余地)。
SPEED_MIN = -100
SPEED_MAX = 100
SPEED_NEUTRAL_US = 1500
SPEED_HALF_SPAN_US = 500

_DEFAULT_TIMEOUT_SEC = 10.0


def _addon_params() -> Dict[str, Any]:
    try:
        from saiverse.addon_config import get_params

        return get_params(ADDON_NAME) or {}
    except Exception:
        LOGGER.exception("servo8: failed to load AddonConfig params")
        return {}


def _vessel_building_id() -> Optional[str]:
    vbid = _addon_params().get("vessel_building_id")
    return str(vbid) if vbid else None


def _unit_enabled() -> bool:
    val = _addon_params().get("unit_servo8_enabled", False)
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


async def _call_i2c_write(addr: int, write_bytes: List[int]) -> str:
    conn = _get_mcp_connection()
    return await conn.call_tool(MCP_TOOL_WRITE, {"addr": addr, "bytes": write_bytes})


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
        raise RuntimeError("MCP event loop is not initialized")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout_sec)


# ============================================================
# Hub lazy recovery (env3.py と同じ方針)
# ============================================================
# Port A に PaHUB を挟む構成では、 ハブの全 channel が closed 状態
# (= power-on default / Stack-chan 再起動後 / ハブ付け替え直後) で I2C が失敗
# する。 各操作で 1 回目試行 → ESP_ERR_* なら hub.open_all_channels() →
# 2 回目試行、 の lazy recovery に乗せる。 直結 (hub_type=none) なら機構自体
# スキップして既存挙動と等価。

def _payload_has_esp_err(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("ok"):
        return False
    err = payload.get("error", "")
    return isinstance(err, str) and err.startswith("ESP_ERR_")


async def _execute_with_hub_recovery(
    operation: Callable[[], Awaitable[Any]],
    is_i2c_failure: Callable[[Any], bool],
) -> Any:
    hub: Optional[PaHub] = get_pahub_from_params()
    result = await operation()

    if hub is None or not is_i2c_failure(result):
        return result

    LOGGER.info(
        "servo8: I2C failure on hub-routed access, "
        "attempting PaHub recovery (open all channels)"
    )
    recovered = await hub.open_all_channels()
    if not recovered:
        LOGGER.warning(
            "servo8: PaHub recovery (open_all_channels) failed; "
            "returning original i2c error to caller"
        )
        return result

    LOGGER.info("servo8: PaHub recovery succeeded, retrying operation once")
    return await operation()


# ============================================================
# 共通: MODE → 値 の 2 段書き込み
# ============================================================

async def _ensure_mode_and_write(
    channel: int, value_bytes: List[int]
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """1 チャンネルを SERVO_CTL_MODE にしてから ``value_bytes`` を書き込む。

    ``value_bytes`` は ``[reg, data...]`` 形式 (角度なら [ANGLE_REG+ch, angle]、
    パルスなら [PULSE_REG+ch*2, lo, hi])。

    Returns ``(mode_payload, value_payload)``。 mode 書き込みが失敗したら
    ``(mode_payload, None)`` を返して値は書かない。
    """
    mode_rendered = await _call_i2c_write(
        SERVO8_ADDR, [MODE_REG + channel, SERVO_CTL_MODE]
    )
    mode_payload = _parse_i2c_payload(mode_rendered)
    if mode_payload is None or not mode_payload.get("ok"):
        return mode_payload, None

    value_rendered = await _call_i2c_write(SERVO8_ADDR, value_bytes)
    value_payload = _parse_i2c_payload(value_rendered)
    return mode_payload, value_payload


def _seq_is_i2c_failure(
    result: Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]],
) -> bool:
    mode_payload, value_payload = result
    return _payload_has_esp_err(mode_payload) or _payload_has_esp_err(value_payload)


def _run_servo_write(channel: int, value_bytes: List[int]) -> Tuple[
    Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[str]
]:
    """``_ensure_mode_and_write`` を hub recovery 付きで同期実行する共通口。

    Returns ``(mode_payload, value_payload, exc_message)``。 例外時は
    ``(None, None, msg)``。
    """
    try:
        mode_payload, value_payload = _run_on_mcp_loop(
            _execute_with_hub_recovery(
                lambda: _ensure_mode_and_write(channel, value_bytes),
                _seq_is_i2c_failure,
            )
        )
        return mode_payload, value_payload, None
    except Exception as exc:
        LOGGER.exception("servo8: I2C sequence failed (ch=%d)", channel)
        return None, None, str(exc)


def _format_write_error(
    channel: int,
    mode_payload: Optional[Dict[str, Any]],
    value_payload: Optional[Dict[str, Any]],
    exc_message: Optional[str],
    value_label: str,
) -> Optional[str]:
    """書き込み結果からユーザー向けエラー文字列を組み立てる (成功なら None)。"""
    if exc_message is not None:
        return f"サーボの{value_label}設定に失敗しました (I2C 通信エラー): {exc_message}"

    if mode_payload is None or not mode_payload.get("ok"):
        err = (
            mode_payload.get("error", "unknown")
            if isinstance(mode_payload, dict)
            else "no response"
        )
        LOGGER.warning("servo8: MODE write failed (ch=%d): %s", channel, err)
        return (
            f"サーボ (ch{channel}) をサーボモードに設定できませんでした: {err}。 "
            "Port A への 8Servos Unit 接続状態を確認してください。"
        )

    if value_payload is None or not value_payload.get("ok"):
        err = (
            value_payload.get("error", "unknown")
            if isinstance(value_payload, dict)
            else "no response"
        )
        LOGGER.warning("servo8: %s write failed (ch=%d): %s", value_label, channel, err)
        return (
            f"サーボ (ch{channel}) に{value_label}を書き込めませんでした: {err}。 "
            "Port A への 8Servos Unit 接続状態を確認してください。"
        )

    return None


def _validate_channel(channel: Any) -> Tuple[Optional[int], Optional[str]]:
    try:
        channel = int(channel)
    except (TypeError, ValueError):
        return None, f"channel は整数で指定してください (指定値 {channel!r})。"
    if not 0 <= channel < NUM_CHANNELS:
        return None, f"channel は 0〜{NUM_CHANNELS - 1} で指定してください (指定値 {channel})。"
    return channel, None


# ============================================================
# servo8_set_angle (180° 位置決めサーボ向け)
# ============================================================

def servo8_set_angle(channel: int = 0, angle: int = 90) -> str:
    """M5Stack 8Servos Unit の指定チャンネルのサーボを指定角に動かす (180° サーボ向け)。

    Args:
        channel: サーボのチャンネル番号 (0〜7)。 物理配線でどのチャンネルに
            何のサーボが繋がっているかはユーザーの構成次第。
        angle: 目標角 (度)。 0〜180。

    Returns:
        設定結果を表す客観的なテキスト。
    """
    if not _unit_enabled():
        return (
            "8Servos Unit は無効化されています。 アドオン管理 UI で「8Servos "
            "Unit (サーボドライバ) を有効化」 を ON にしてください。"
        )

    channel, err = _validate_channel(channel)
    if err is not None:
        return err

    try:
        angle = int(angle)
    except (TypeError, ValueError):
        return f"angle は整数で指定してください (指定値 {angle!r})。"
    if not ANGLE_MIN <= angle <= ANGLE_MAX:
        return f"angle は {ANGLE_MIN}〜{ANGLE_MAX} 度で指定してください (指定値 {angle})。"

    mode_payload, value_payload, exc_message = _run_servo_write(
        channel, [ANGLE_REG + channel, angle]
    )
    error = _format_write_error(channel, mode_payload, value_payload, exc_message, "角度")
    if error is not None:
        return error

    LOGGER.info("servo8.set_angle: ch=%d angle=%d° set", channel, angle)
    return f"サーボ (ch{channel}) を {angle}° に設定しました。"


# ============================================================
# servo8_set_speed (360° 連続回転サーボ向け)
# ============================================================

def _speed_to_pulse_us(speed: int) -> int:
    """速度 (-100〜100) を連続回転サーボのパルス幅 (µs) に変換。 0 = 中立 (停止)。"""
    return SPEED_NEUTRAL_US + round(speed / SPEED_MAX * SPEED_HALF_SPAN_US)


def servo8_set_speed(channel: int = 0, speed: int = 0) -> str:
    """M5Stack 8Servos Unit の指定チャンネルの連続回転サーボの回転速度を設定する。

    360° (連続回転) サーボ向け。 速度は -100〜100 で、 0 が停止、 正負で
    回転方向が変わる (どちらが前進かはサーボの取り付け向き次第)。 一度設定すると
    その速度で回り続けるので、 止めるときは speed=0 を呼ぶ。 「○秒だけ回す」
    のような動作は複合アクションで (回す → 待つ → 止める) と組む。

    Args:
        channel: サーボのチャンネル番号 (0〜7)。
        speed: 回転速度 (-100〜100)。 0 = 停止、 100 = 一方向に最大、
            -100 = 逆方向に最大。

    Returns:
        設定結果を表す客観的なテキスト。
    """
    if not _unit_enabled():
        return (
            "8Servos Unit は無効化されています。 アドオン管理 UI で「8Servos "
            "Unit (サーボドライバ) を有効化」 を ON にしてください。"
        )

    channel, err = _validate_channel(channel)
    if err is not None:
        return err

    try:
        speed = int(speed)
    except (TypeError, ValueError):
        return f"speed は整数で指定してください (指定値 {speed!r})。"
    if not SPEED_MIN <= speed <= SPEED_MAX:
        return f"speed は {SPEED_MIN}〜{SPEED_MAX} で指定してください (指定値 {speed})。"

    pulse_us = _speed_to_pulse_us(speed)
    value_bytes = [PULSE_REG + channel * 2, pulse_us & 0xFF, (pulse_us >> 8) & 0xFF]

    mode_payload, value_payload, exc_message = _run_servo_write(channel, value_bytes)
    error = _format_write_error(channel, mode_payload, value_payload, exc_message, "速度")
    if error is not None:
        return error

    LOGGER.info(
        "servo8.set_speed: ch=%d speed=%d (pulse=%dµs) set", channel, speed, pulse_us
    )
    if speed == 0:
        return f"サーボ (ch{channel}) を停止しました。"
    return f"サーボ (ch{channel}) の回転速度を {speed} に設定しました (停止は speed=0)。"


# ============================================================
# Spell registry
# ============================================================

def _build_schema(name: str, description: str, display_name: str, parameters: Dict[str, Any]) -> ToolSchema:
    vbid = _vessel_building_id()
    enabled = _unit_enabled()
    visible = bool(enabled and vbid)
    building_ids = [vbid] if vbid else None
    return ToolSchema(
        name=name,
        description=description,
        parameters=parameters,
        result_type="string",
        spell=True,
        spell_display_name=display_name,
        spell_visible=visible,
        building_ids=building_ids,
    )


def schemas() -> List[ToolSchema]:
    """1 ファイル複数 spell の登録 entry point (env3.py と同形、 ``schemas()`` 複数形)。

    ``spell_visible`` は AddonConfig の ``unit_servo8_enabled`` + 有効な
    ``vessel_building_id`` が両方揃った時だけ True。 schemas() は spell surface
    構築のたびに呼ばれるので、 toggle 切り替え後の reconnect で即時に visibility
    が反映される (= subprocess restart 不要)。
    """
    enabled = _unit_enabled()
    vbid = _vessel_building_id()
    if not enabled:
        LOGGER.debug("servo8: unit_servo8_enabled is false; spells hidden from persona")
    elif vbid is None:
        LOGGER.warning(
            "servo8: vessel_building_id not configured; spells will not be visible"
        )

    return [
        _build_schema(
            name="servo8_set_angle",
            description=(
                "あなたの身体 (Stack-chan) に接続された M5Stack 8Servos Unit の指定"
                " チャンネル (0〜7) の 180° サーボを指定角度 (0〜180度) に動かす。"
                " 腕・首など向きを決めるサーボ用。 どのチャンネルに何のサーボが"
                " 繋がっているかは物理配線で決まる。 連続回転 (車輪) には"
                " servo8_set_speed を使う。"
            ),
            display_name="サーボの角度を設定",
            parameters={
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "integer",
                        "description": "サーボのチャンネル番号 (0〜7)。",
                        "minimum": 0,
                        "maximum": NUM_CHANNELS - 1,
                    },
                    "angle": {
                        "type": "integer",
                        "description": "目標角 (度)。 0〜180。",
                        "minimum": ANGLE_MIN,
                        "maximum": ANGLE_MAX,
                    },
                },
                "required": ["channel", "angle"],
            },
        ),
        _build_schema(
            name="servo8_set_speed",
            description=(
                "あなたの身体 (Stack-chan) に接続された M5Stack 8Servos Unit の指定"
                " チャンネル (0〜7) の 360° 連続回転サーボ (車輪など) の回転速度を"
                " 設定する。 speed は -100〜100 で 0 が停止、 正負で回転方向が変わる。"
                " 一度設定するとその速度で回り続けるので、 止めるときは speed=0 を"
                " 呼ぶ。 「少し前進」 のような動作は複合アクション (回す→待つ→止める)"
                " として定義しておくと名前で呼べる。"
            ),
            display_name="サーボの回転速度を設定",
            parameters={
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "integer",
                        "description": "サーボのチャンネル番号 (0〜7)。",
                        "minimum": 0,
                        "maximum": NUM_CHANNELS - 1,
                    },
                    "speed": {
                        "type": "integer",
                        "description": "回転速度 (-100〜100)。 0=停止、 ±100=最大速度。",
                        "minimum": SPEED_MIN,
                        "maximum": SPEED_MAX,
                    },
                },
                "required": ["channel", "speed"],
            },
        ),
    ]
