"""Read a human-oriented, head-angle-compensated Stack-chan IMU snapshot."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sys
from pathlib import Path
from typing import Any

from tools.core import ToolSchema

_ADDON_ROOT = str(Path(__file__).resolve().parent.parent)
if _ADDON_ROOT not in sys.path:
    sys.path.insert(0, _ADDON_ROOT)

from vessel_dispatch import (  # noqa: E402
    building_gate_or_hidden,
    list_vessel_building_ids,
    resolve_vessel_connection,
)

LOGGER = logging.getLogger(__name__)

MCP_TOOL_READ_IMU = "read_imu"
MCP_TOOL_GET_HEAD_ANGLES = "get_head_angles"
_DEFAULT_TIMEOUT_SEC = 15.0
_HEAD_NEUTRAL_PITCH_DEG = 45.0

_CARDINAL_DIRECTIONS = (
    "北",
    "北北東",
    "北東",
    "東北東",
    "東",
    "東南東",
    "南東",
    "南南東",
    "南",
    "南南西",
    "南西",
    "西南西",
    "西",
    "西北西",
    "北西",
    "北北西",
)


def _run_on_mcp_loop(coro, timeout_sec: float = _DEFAULT_TIMEOUT_SEC) -> Any:
    """Bridge a synchronous native spell call to the MCP event loop."""
    import tools.mcp_client as mcp_client

    loop = mcp_client._loop
    if loop is None:
        coro.close()
        raise RuntimeError("MCP event loop is not initialized")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout_sec)


async def _call_context() -> tuple[str, str]:
    """Read the servo angles and IMU from the same selected vessel."""
    _vessel, connection = resolve_vessel_connection()
    angles = await connection.call_tool(MCP_TOOL_GET_HEAD_ANGLES, {})
    imu = await connection.call_tool(MCP_TOOL_READ_IMU, {})
    return angles, imu


def _parse_payload(rendered: Any) -> dict[str, Any] | None:
    if isinstance(rendered, dict):
        return rendered
    if not isinstance(rendered, str):
        return None
    try:
        value = json.loads(rendered)
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _number(payload: dict[str, Any], key: str) -> float | None:
    value = payload.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _round(value: float, digits: int = 3) -> float:
    return round(value, digits)


def _rotate_sensor_to_body(
    vector: dict[str, Any], yaw_deg: float, pitch_deg: float
) -> dict[str, float]:
    """Rotate sensor xyz into the body frame used by this spell.

    CoreS3's board-facing convention is x=right, y=up (legs to head), and
    z=forward (camera-facing) when the head is at its neutral pitch of 45°.
    The servo pitch is absolute (0° looks down, 90° looks up), so rotate by
    ``pitch - 45°`` before applying yaw. Positive StackChan yaw turns left.
    This is an explicit first-pass convention; future per-device calibration
    can replace it without changing the output contract.
    """
    x = float(vector.get("x", 0.0))
    sensor_up = float(vector.get("y", 0.0))
    sensor_forward = float(vector.get("z", 0.0))
    pitch = math.radians(pitch_deg - _HEAD_NEUTRAL_PITCH_DEG)
    yaw = math.radians(yaw_deg)

    # First map the head's y/up and z/forward axes through pitch. At
    # pitch=45° this is the identity; at pitch=90° the camera-forward axis
    # points upward. Then apply the horizontal yaw (positive yaw = left).
    head_forward = math.cos(pitch) * sensor_forward - math.sin(pitch) * sensor_up
    head_up = math.sin(pitch) * sensor_forward + math.cos(pitch) * sensor_up
    body_right = math.cos(yaw) * x - math.sin(yaw) * head_forward
    body_forward = math.sin(yaw) * x + math.cos(yaw) * head_forward
    return {
        "right": body_right,
        "forward": body_forward,
        "up": head_up,
    }


def _relative_direction(angle_deg: float) -> str:
    if abs(angle_deg) < 22.5:
        return "前"
    if abs(angle_deg) > 157.5:
        return "後ろ"
    if angle_deg > 0:
        return "右前" if angle_deg < 67.5 else "右"
    return "左前" if angle_deg > -67.5 else "左"


def _cardinal(heading_deg: float) -> str:
    index = int((heading_deg + 11.25) // 22.5) % len(_CARDINAL_DIRECTIONS)
    return _CARDINAL_DIRECTIONS[index]


def _build_result(
    imu: dict[str, Any], angles: dict[str, Any] | None, angle_note: str | None
) -> dict[str, Any]:
    head_yaw = _number(angles or {}, "yaw")
    head_pitch = _number(angles or {}, "pitch")
    if head_yaw is None or head_pitch is None:
        head_yaw = 0.0
        # Without a servo read, keep the sensor axes at the physical neutral
        # pose rather than applying the 0° (look-down) pitch transform.
        head_pitch = _HEAD_NEUTRAL_PITCH_DEG

    accel = imu.get("accel_g")
    gyro = imu.get("gyro_dps")
    mag = imu.get("mag_ut")
    if not isinstance(accel, dict) or not isinstance(gyro, dict):
        raise ValueError("IMU payload is missing accel_g or gyro_dps")

    body_accel = _rotate_sensor_to_body(accel, head_yaw, head_pitch)
    body_mag = _rotate_sensor_to_body(mag, head_yaw, head_pitch) if isinstance(mag, dict) else None
    horizontal_accel = math.hypot(body_accel["right"], body_accel["forward"])
    accel_magnitude = math.sqrt(sum(value * value for value in body_accel.values()))
    accel_direction = math.degrees(
        math.atan2(body_accel["right"], body_accel["forward"])
    )
    tilt = math.degrees(math.atan2(horizontal_accel, abs(body_accel["up"])))

    magnetic_heading: dict[str, Any]
    if body_mag is None:
        magnetic_heading = {"heading_deg": None, "direction": None}
    else:
        mag_horizontal = math.hypot(body_mag["right"], body_mag["forward"])
        if mag_horizontal < 1e-6:
            magnetic_heading = {"heading_deg": None, "direction": None}
        else:
            # Magnetic north points toward the north component of the field. In
            # a right/forward body frame, the device heading is atan2(-right,
            # forward): east-facing means north is on the body's left.
            heading = math.degrees(
                math.atan2(-body_mag["right"], body_mag["forward"])
            ) % 360.0
            magnetic_heading = {
                "heading_deg": _round(heading, 1),
                "direction": _cardinal(heading),
            }

    result: dict[str, Any] = {
        "head_angles_deg": {
            "yaw": _round(head_yaw, 1),
            "pitch": _round(head_pitch, 1),
        },
        "acceleration": {
            "frame": "脚側（胴体）基準。right=右、forward=カメラ前方、up=脚から頭方向",
            "vector_g": {key: _round(value) for key, value in body_accel.items()},
            "magnitude_g": _round(accel_magnitude),
            "horizontal": {
                "magnitude_g": _round(horizontal_accel),
                "direction_deg": _round(accel_direction, 1),
                "direction": _relative_direction(accel_direction),
                "direction_reference": "0°=カメラ前方、+90°=右、-90°=左",
            },
            "tilt_from_vertical_deg": _round(tilt, 1),
        },
        "magnetic_heading": magnetic_heading,
        "angular_velocity_dps": {
            key: _round(float(gyro.get(key, 0.0))) for key in ("x", "y", "z")
        },
    }

    notes: list[str] = [
        "磁気方位は未校正（ハードアイアン・ソフトアイアン・磁気偏角補正なし、機体ロール未補正）の推定値です。"
    ]
    if angle_note:
        notes.append(angle_note)
    if imu.get("mag_available") is False:
        notes.append("磁力計が利用できないため、磁気方位は無効です。")
    data_ready = imu.get("data_ready")
    if isinstance(data_ready, dict):
        not_ready = [key for key in ("accel", "gyro", "mag") if data_ready.get(key) is False]
        if not_ready:
            notes.append(f"データ未準備: {', '.join(not_ready)}")
    result["notes"] = notes
    return result


def read_imu_context() -> str:
    """Return a head-angle-compensated, persona-readable IMU snapshot."""
    try:
        rendered_angles, rendered_imu = _run_on_mcp_loop(_call_context())
    except Exception as exc:
        LOGGER.exception("read_imu_context: MCP call failed")
        return f"IMUの身体感覚を取得できませんでした: {exc}"

    imu = _parse_payload(rendered_imu)
    if not imu:
        LOGGER.warning("read_imu_context: invalid IMU payload")
        return "IMUの身体感覚を取得できませんでした: IMU応答が不正です"
    if imu.get("ok") is False or "error" in imu:
        error = imu.get("error", "unknown IMU error")
        return f"IMUの身体感覚を取得できませんでした: {error}"

    angles = _parse_payload(rendered_angles)
    angle_note = None
    if (
        not angles
        or angles.get("yaw") is None
        or angles.get("pitch") is None
        or angles.get("servo_ok") is False
    ):
        angle_note = "首角度を取得できないため、センサー軸を脚側基準として扱いました。"
        angles = None
    elif "error" in angles:
        angle_note = "首角度の取得に失敗したため、センサー軸を脚側基準として扱いました。"
        angles = None

    try:
        result = _build_result(imu, angles, angle_note)
    except (TypeError, ValueError, KeyError) as exc:
        LOGGER.exception("read_imu_context: payload conversion failed")
        return f"IMUの身体感覚を解釈できませんでした: {exc}"
    LOGGER.info("read_imu_context: collected compensated IMU context")
    return json.dumps(result, ensure_ascii=False)


def schema() -> ToolSchema:
    building_ids, visible = building_gate_or_hidden(list_vessel_building_ids())
    if not visible:
        LOGGER.info(
            "read_imu_context: no vessel registered yet; tool hidden everywhere until pairing."
        )
    return ToolSchema(
        name="read_imu_context",
        description=(
            "現在のStack-chanのIMUを、首のyaw/pitchで脚側（胴体）基準へ補正して読む。"
            "加速度は水平面の方向・大きさ・傾き、磁力計は磁気北からの推定方位、"
            "角速度はdpsで返す。診断用のraw値やアドレスは返さず、補正不能・未準備・"
            "未校正など解釈に影響する状態だけ注記する。"
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        result_type="string",
        spell=True,
        spell_display_name="身体の向きと加速度を読む",
        spell_visible=visible,
        building_ids=building_ids,
    )
