"""M5Stack PaHUB / PaHUB 2 (TCA9548A) thin ドライバ。

Stack-chan の Grove Port A 配下に I2C MUX (PaHUB / PaHUB 2) を挟んで複数
Unit を繋ぐ構成で、 各 Unit driver が共通で使う最小機能の hub 抽象層。

現状は **全 channel 同時 open (= 制御 register に 0xFF を書く)** だけを行い、
各 Unit (ENV III の SHT30 0x44 / QMP6988 0x70 等) は trunk から直結時と同じ
アドレスで叩く。 同じ channel 内に同一 address Unit を複数置く・ 別 channel
に同 address Unit を挿す要求が出てきたら、 per-channel select API を追加
する想定 (= future work)。

設計判断: 「起動時 1 回 init」 ではなく、 Unit driver 側が i2c-level
failure を検出した時に open_all_channels() を呼ぶ **lazy recovery** 方式を
採用する。 これにより以下のいずれが起きてもユーザー操作不要で復帰する:

  - Python プロセス起動直後 (= PaHUB は power-on で全 channel closed)
  - Stack-chan 再起動 (= PaHUB も電源切断で register リセット)
  - ハブを物理的に付け替えた直後
  - 何らかの偶発的 state 変化

TI 公式 (TCA9548A datasheet / product page) 仕様確認:
  - "Any individual SCn/SDn channel or combination of channels can be selected"
  - "Power up with all switch channels deselected"
  - 制御 register 1 byte、 bit position が channel mask (Adafruit
    CircuitPython driver の `1 << channel` 実装で裏取り)
  - I2C address 0x70-0x77 (A0/A1/A2 ピンで選択)

参照:
  - docs/intent/stackchan_extension_modules.md「I2C MUX (PaHUB) 対応」 節
  - https://www.ti.com/product/TCA9548A
"""

import json
import logging
from typing import Any, Dict, Optional

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"
MCP_QUALIFIED_SERVER = f"{ADDON_NAME}__stackchan"
MCP_TOOL_WRITE = "i2c_write"

# TCA9548A 制御 register に書く値: 0xFF = 全 8 channel 同時 open
_ALL_CHANNELS_OPEN = 0xFF


class PaHub:
    """M5Stack PaHUB / PaHUB 2 (TCA9548A) 用最小ドライバ。

    Args:
        address: ハブの I2C アドレス (0x70-0x77)。 物理デバイスの A0/A1/A2
            パッド状態から ``get_pahub_from_params()`` が組み立てる。
    """

    def __init__(self, address: int):
        if not 0x70 <= address <= 0x77:
            raise ValueError(
                f"PaHUB address must be in 0x70..0x77 (got 0x{address:02X})"
            )
        self.address = address

    async def open_all_channels(self) -> bool:
        """全 8 channel を 1 回の I2C write で open する。

        Idempotent (= 何度呼んでも結果は同じ)。 失敗しても例外を投げず False を
        返す (呼び元側のリトライ判断を妨げない)。

        Returns:
            i2c_write が gateway から ok=true で返ってきたら True、 それ以外 False。
        """
        conn = _get_mcp_connection()
        if conn is None:
            LOGGER.warning(
                "pahub.open_all_channels: MCP connection not available"
            )
            return False

        try:
            rendered = await conn.call_tool(
                MCP_TOOL_WRITE,
                {"addr": self.address, "bytes": [_ALL_CHANNELS_OPEN]},
            )
        except Exception:
            LOGGER.exception(
                "pahub.open_all_channels: i2c_write to 0x%02X raised",
                self.address,
            )
            return False

        payload = _parse_i2c_payload(rendered)
        ok = bool(payload and payload.get("ok"))
        if not ok:
            err = (
                payload.get("error", "unknown")
                if isinstance(payload, dict)
                else "no response"
            )
            LOGGER.warning(
                "pahub.open_all_channels: i2c_write to 0x%02X failed: %s",
                self.address,
                err,
            )
        else:
            LOGGER.info(
                "pahub.open_all_channels: addr=0x%02X all channels opened",
                self.address,
            )
        return ok


def _parse_i2c_payload(rendered: Any) -> Optional[Dict[str, Any]]:
    """gateway 経由の i2c_* tool レスポンス JSON をパース。"""
    if rendered is None:
        return None
    try:
        payload = json.loads(rendered)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _get_mcp_connection() -> Optional[Any]:
    """SAIVerse 本体 MCP client の stackchan-mcp gateway connection を取得。"""
    try:
        from tools.mcp_client import _make_instance_key, get_mcp_manager
    except Exception:
        LOGGER.exception("pahub: failed to import MCP client module")
        return None

    try:
        manager = get_mcp_manager()
        if manager is None:
            return None
        instance_key = _make_instance_key(MCP_QUALIFIED_SERVER, persona_id=None)
        return manager._connections.get(instance_key)
    except Exception:
        LOGGER.exception("pahub: failed to acquire MCP connection")
        return None


def _addon_params() -> Dict[str, Any]:
    try:
        from saiverse.addon_config import get_params

        return get_params(ADDON_NAME) or {}
    except Exception:
        LOGGER.exception("pahub: failed to load AddonConfig params")
        return {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes", "on")
    return bool(value)


def get_pahub_from_params() -> Optional[PaHub]:
    """AddonConfig (hub_type / hub_addr_a*) から PaHub インスタンスを組み立てる。

    hub_type が "pahub" 以外なら None (= ハブなし直結構成と解釈)。 PaHUB の
    I2C address は ``0x70 | (a2<<2) | (a1<<1) | a0`` で組み立てる (物理
    パッド A0/A1/A2 の High/Low 状態がそのままアドレス bit にマップされる)。

    Returns:
        hub_type=pahub の場合に組み立てた PaHub、 それ以外は None。
    """
    params = _addon_params()
    hub_type = str(params.get("hub_type", "none")).lower()
    if hub_type != "pahub":
        return None

    a0 = 1 if _truthy(params.get("hub_addr_a0")) else 0
    a1 = 1 if _truthy(params.get("hub_addr_a1")) else 0
    a2 = 1 if _truthy(params.get("hub_addr_a2")) else 0
    address = 0x70 | (a2 << 2) | (a1 << 1) | a0
    LOGGER.debug(
        "pahub: resolved address 0x%02X from params "
        "(A0=%d A1=%d A2=%d)",
        address, a0, a1, a2,
    )
    return PaHub(address=address)
