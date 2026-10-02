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

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

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

    async def select_channel(self, channel: int) -> bool:
        """指定 channel **だけ**を open にする (他 channel は close)。

        制御 register に ``1 << channel`` を書く。同一 I2C アドレスの Unit を
        別 channel に挿した構成 (VL53L1X ×2 等) で、対象 channel だけをバスに
        出して衝突を防ぐ (docs/intent/stackchan_unit_placement.md §4)。
        Idempotent。失敗しても例外は投げず False を返す。
        """
        if not 0 <= channel <= 7:
            LOGGER.warning(
                "pahub.select_channel: channel out of range (0-7): %r", channel
            )
            return False
        conn = _get_mcp_connection()
        if conn is None:
            LOGGER.warning("pahub.select_channel: MCP connection not available")
            return False
        mask = 1 << channel
        try:
            rendered = await conn.call_tool(
                MCP_TOOL_WRITE, {"addr": self.address, "bytes": [mask]}
            )
        except Exception:
            LOGGER.exception(
                "pahub.select_channel: i2c_write to 0x%02X raised", self.address
            )
            return False
        payload = _parse_i2c_payload(rendered)
        ok = bool(payload and payload.get("ok"))
        if ok:
            LOGGER.debug(
                "pahub.select_channel: addr=0x%02X channel=%d selected "
                "(mask=0x%02X)", self.address, channel, mask,
            )
        else:
            err = (
                payload.get("error", "unknown")
                if isinstance(payload, dict) else "no response"
            )
            LOGGER.warning(
                "pahub.select_channel: addr=0x%02X ch=%d failed: %s",
                self.address, channel, err,
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
    """現在ペルソナが降りている機体の gateway connection を取得 (per-vessel)。

    複数機体対応: 旧実装は ``_make_instance_key(..., persona_id=None)`` で
    global インスタンスを引いていたが、 instance_template scope では global は
    存在せず常に None になり、 PaHub の channel open が失敗していた (= env3 等
    ハブ経由ユニットが「Port A 接続エラー」になる主因)。 env3.py と同じ
    ``vessel_dispatch.resolve_vessel_connection`` で現在機体へ解決する。 未解決時
    は None を返し、 呼び出し側 (open_all_channels) が復帰失敗として扱う。
    """
    import sys
    from pathlib import Path

    # addon root (vessel_dispatch.py) を import 可能にする (hubs/ の 2 つ上)。
    _addon_root = str(Path(__file__).resolve().parents[2])
    if _addon_root not in sys.path:
        sys.path.insert(0, _addon_root)

    try:
        from vessel_dispatch import (
            VesselNotAvailable,
            resolve_vessel_connection,
        )
    except Exception:
        LOGGER.exception("pahub: failed to import vessel_dispatch")
        return None

    try:
        _vessel, conn = resolve_vessel_connection()
        return conn
    except VesselNotAvailable:
        return None
    except Exception:
        LOGGER.exception("pahub: failed to acquire MCP connection")
        return None
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


# ============================================================
# per-vessel hub 解決 + channel ルーティング (v0.11 unit placement)
# ============================================================
# docs/intent/stackchan_unit_placement.md §3/§4。per-vessel の unit_config.hub を
# 優先し、無ければグローバル params にフォールバックする。unit driver は「自分の
# channel を渡す」だけで select の有無を意識しない (不変条件 3)。


def _parse_addr(val: Any) -> Optional[int]:
    """hub addr を int に正規化する ("0x71" / 113 の両方を受ける)。"""
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        try:
            return int(val, 16) if val.lower().startswith("0x") else int(val)
        except ValueError:
            return None
    return None


def get_pahub_for_vessel(vessel: Any) -> Optional[PaHub]:
    """vessel の有効な hub 設定から PaHub を組み立てる (ハブ無しなら None)。

    per-vessel の ``unit_config.hub`` があればそれを使い、無ければグローバル
    params (legacy) にフォールバックする (docs/intent/stackchan_unit_placement.md
    §3)。``type != pahub`` / アドレス不正なら None。
    """
    cfg = getattr(vessel, "unit_config", None)
    if isinstance(cfg, dict) and isinstance(cfg.get("hub"), dict):
        hub = cfg["hub"]
        if str(hub.get("type") or "none").lower() != "pahub":
            return None
        addr = _parse_addr(hub.get("addr"))
        if addr is None or not 0x70 <= addr <= 0x77:
            LOGGER.warning(
                "pahub: invalid hub addr in unit_config: %r", hub.get("addr")
            )
            return None
        return PaHub(address=addr)
    # per-vessel 設定が無ければグローバル params にフォールバック (legacy)
    return get_pahub_from_params()


# vessel (= 1 gateway = 1 Port A バス) ごとの channel 選択直列化ロック。
# 値は (束縛先ループ, Lock)。 asyncio.Lock は初回 acquire したループに束縛され、
# 別ループから使うと RuntimeError になるため、 束縛先ループを覚えておき、 (稀に)
# MCP ループが作り直されたら現在ループ用に作り直す。
_hub_locks: Dict[str, Tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def get_hub_lock(vessel_id: str) -> asyncio.Lock:
    """vessel ごとの「channel 選択 + 測定」直列化ロックを返す (要 running loop)。

    ハブの channel 選択はバス全体の状態 (現在 open な channel は 1 つ) なので、
    複数ユニットの測定が並列に走ると「select ch4 → select ch3 → read → read」と
    互い違いになり、 両方が最後に選択された channel を読んでしまう
    (= 2 つの ToF が同じ値・同じエラーを返す)。 select + 測定シーケンス全体を本
    ロックで括ることで、 1 ユニットの測定が終わるまで別ユニットが channel を
    切り替えないようにする。 直結 (hub なし) は競合しないのでロック不要。
    """
    loop = asyncio.get_running_loop()
    entry = _hub_locks.get(vessel_id)
    if entry is None or entry[0] is not loop:
        lock = asyncio.Lock()
        _hub_locks[vessel_id] = (loop, lock)
        return lock
    return entry[1]


async def route_to_channel(hub: Optional[PaHub], channel: Optional[int]) -> bool:
    """ユニットの I2C 前に呼ぶ: 対象 channel をバスに出す。

    - hub None (直結): 何もしない (True)。
    - channel が int: その channel **だけ** select (同アドレス衝突を防ぐ)。
    - channel が None (配置情報の無い旧構成): 全 channel open にフォールバック
      (= 従来挙動。1 unit / 1 channel の典型構成なら衝突しない)。
    """
    if hub is None:
        return True
    if channel is not None:
        return await hub.select_channel(channel)
    return await hub.open_all_channels()


async def execute_with_hub_recovery(
    operation: Callable[[], Awaitable[Any]],
    is_i2c_failure: Callable[[Any], bool],
    unit_cap_key: str,
) -> Any:
    """ハブ経由ユニットの i2c 操作を channel select + lazy recovery 付きで実行。

    env3 / sonic / servo8 共通 (tof は例外ベースなので別経路)。現在 vessel の
    ``unit_cap_key`` ユニット (最初の 1 件) の channel を解決 →
    :func:`route_to_channel` でルーティング → ``operation`` 実行。ハブ経由で
    i2c-level failure なら再ルーティングして **1 回だけ** 再試行する。直結
    (hub None) なら 1 回実行してそのまま返す。

    Args:
        operation: 引数なし async コール (測定 sequence)。
        is_i2c_failure: 操作結果が「ハブ起因リカバリ対象か」を判定する callable。
        unit_cap_key: このユニットの capability キー (= ``MY_UNIT_CAP_KEY``)。
    """
    from vessel_dispatch import resolve_vessel, units_of_type

    vessel = resolve_vessel()
    hub = get_pahub_for_vessel(vessel)
    units = units_of_type(vessel, unit_cap_key)
    channel = units[0]["channel"] if units else None

    # 直結 (ハブなし) は channel 切替も競合も無いのでロック不要で 1 回実行。
    if hub is None:
        return await operation()

    async def _routed_once() -> Any:
        await route_to_channel(hub, channel)
        result = await operation()
        if not is_i2c_failure(result):
            return result
        LOGGER.info(
            "pahub: i2c failure for '%s' (channel=%s), re-routing and retrying once",
            unit_cap_key, channel,
        )
        if not await route_to_channel(hub, channel):
            # 再ルーティング自体が通らない (結線 / アドレス) → 元エラーを返す
            return result
        return await operation()

    # 別ユニットの測定が select+read の途中で channel を切り替えないよう、
    # vessel ごとのロックで「select → 測定」を直列化する (get_hub_lock 参照)。
    async with get_hub_lock(vessel.vessel_id):
        return await _routed_once()
