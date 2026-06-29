# Stack-chan Unit driver 作成ガイド

このディレクトリ (`tools/units/`) は Stack-chan の Grove Port A に接続する I2C Unit (M5Stack ENV III / SGP30 / ToF / PaHub / 他) を spell として expose する native tool の置き場です。 新しい Unit を追加するときの手順をまとめています。

## 前提

- **stackchan-mcp firmware** に Port A I2C 汎用 tool (`self.i2c.scan` / `read` / `write` / `write_read`) が実装されていること
  - upstream PR #195 (kPropertyTypeArray) + #196 (Port A bus + I2C tools) の merge 後、 もしくは fork `dev/integration` で先行利用
- Stack-chan device がペアリング済み、 SAIVerse の MCP client が gateway subprocess を正常起動できる状態
- AddonConfig の `vessel_building_id` が設定済み (= Vessel Building 限定 spell の前提)
- Unit を Grove Port A に物理接続済み (PaHub 経由で複数同時 OK、 アドレス衝突に注意)

## 追加手順

### Step 1. AddonConfig に enable toggle を追加

`addon.json` の `params_schema` に Unit ごとの toggle を追加します。

```json
{
    "key": "unit_<name>_enabled",
    "label": "<Display Name> を有効化",
    "description": "Stack-chan の Port A に接続した <Unit 説明> を spell として公開します。 物理 Unit を接続していない場合は無効のままにしてください。",
    "type": "toggle",
    "default": false
}
```

`type: "toggle"` で AddonManager UI に boolean ON/OFF switch が出ます。

### Step 2. `tools/units/<unit>.py` を実装

`env3.py` を参考実装として、 同 dir に新 Unit 用の Python file を作成します。 骨子:

```python
import asyncio
import json
import logging
from typing import Any, Dict, Optional

from tools.core import ToolSchema

ADDON_NAME = "saiverse-stackchan-addon"
MCP_QUALIFIED_SERVER = f"{ADDON_NAME}__stackchan"
LOGGER = logging.getLogger(__name__)

# --- Unit 固有定数 ---
MY_UNIT_ADDR = 0x..               # I2C 7-bit address
MY_UNIT_MEASURE_CMD = [0x.., 0x..]
MY_UNIT_MEASURE_WAIT_SEC = 0.020  # measurement 完了までの wait
MY_UNIT_RESULT_BYTES = N


def _addon_params() -> Dict[str, Any]:
    from saiverse.addon_config import get_params
    return get_params(ADDON_NAME) or {}


def _unit_enabled() -> bool:
    val = _addon_params().get("unit_<name>_enabled", False)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes", "on")
    return bool(val)


def _vessel_building_id() -> Optional[str]:
    vbid = _addon_params().get("vessel_building_id")
    return str(vbid) if vbid else None


def _get_mcp_connection():
    from tools.mcp_client import _make_instance_key, get_mcp_manager
    manager = get_mcp_manager()
    if manager is None:
        raise RuntimeError("MCP manager is not initialized")
    conn = manager._connections.get(
        _make_instance_key(MCP_QUALIFIED_SERVER, persona_id=None)
    )
    if conn is None:
        raise RuntimeError(f"MCP server '{MCP_QUALIFIED_SERVER}' is not connected.")
    return conn


def _run_on_mcp_loop(coro, timeout_sec: float = 10.0) -> Any:
    import tools.mcp_client as _mcp
    return asyncio.run_coroutine_threadsafe(coro, _mcp._loop).result(timeout=timeout_sec)


def _parse_i2c_payload(rendered: str) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(rendered)
    except (json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


async def _measure_my_unit():
    conn = _get_mcp_connection()
    w = _parse_i2c_payload(await conn.call_tool(
        "i2c_write", {"addr": MY_UNIT_ADDR, "bytes": MY_UNIT_MEASURE_CMD}
    ))
    if w is None or not w.get("ok"):
        return w, None
    await asyncio.sleep(MY_UNIT_MEASURE_WAIT_SEC)
    r = _parse_i2c_payload(await conn.call_tool(
        "i2c_read", {"addr": MY_UNIT_ADDR, "n_bytes": MY_UNIT_RESULT_BYTES}
    ))
    return w, r


def get_my_unit_value() -> str:
    """Spell entry point。 戻り値は str (= 4-tuple NG)。"""
    if not _unit_enabled():
        return (
            "<Display Name> は無効化されています。 アドオン管理 UI で"
            " 「<Display Name> を有効化」 を ON にしてください。"
        )

    try:
        write_payload, read_payload = _run_on_mcp_loop(_measure_my_unit())
    except Exception as exc:
        LOGGER.exception("my_unit: measurement sequence failed")
        return f"<Display Name> の測定に失敗しました (I2C 通信エラー): {exc}"

    if write_payload is None or not write_payload.get("ok"):
        err = (
            write_payload.get("error", "unknown")
            if isinstance(write_payload, dict) else "no response"
        )
        return (
            f"<Display Name> に測定コマンドを送信できませんでした: {err}。 "
            "Port A への Unit 接続状態を確認してください。"
        )

    if read_payload is None or not read_payload.get("ok"):
        err = (
            read_payload.get("error", "unknown")
            if isinstance(read_payload, dict) else "no response"
        )
        return (
            f"<Display Name> から測定値を取得できませんでした: {err}。 "
            "Port A への Unit 接続状態を確認してください。"
        )

    raw = read_payload.get("bytes", [])
    # ... datasheet に従って raw → 物理単位に変換 ...
    value = ...
    return f"<項目>: {value:.1f} <単位> (<Display Name>、Stack-chan Port A)。"


def _build_schema(name: str, description: str, display_name: str) -> ToolSchema:
    vbid = _vessel_building_id()
    enabled = _unit_enabled()
    return ToolSchema(
        name=name,
        description=description,
        parameters={"type": "object", "properties": {}, "required": []},
        result_type="string",
        spell=True,
        spell_display_name=display_name,
        spell_visible=bool(enabled and vbid),
        building_ids=[vbid] if vbid else None,
    )


def schemas():
    """1 ファイル複数 spell の登録 entry point (loader 仕様)。"""
    return [
        _build_schema(
            name="get_my_unit_value",
            description=(
                "あなたの身体 (Stack-chan) に接続された <Display Name> から"
                " <測定対象> を取得する。 ..."
            ),
            display_name="<Japanese display>",
        ),
        # 同 Unit で複数 measurement (例: ENV III の温湿度 + 気圧) があれば追加
    ]
```

### Step 3. 直叩きで仮説検証 (= 実装前 + 実装中)

memory `feedback_verify_hypothesis_before_code_change` の通り、 **コード書く前に直叩きで動作を確認** することで、 再起動 → ペルソナテスト → 失敗 → 修正ループの無駄を回避できます。 admin tool-call endpoint (`POST /api/mcp/tool-call`) 経由で curl から I2C tool を直接叩けます。

```bash
# chip ID 確認 (sanity check)
curl -s -X POST http://localhost:8000/api/mcp/tool-call \
  -H "Content-Type: application/json" \
  -d '{"server":"saiverse-stackchan-addon__stackchan","tool_name":"i2c_write_read","arguments":{"addr":<ADDR>,"write_bytes":[<CHIP_ID_REG>],"n_bytes":1},"persona_id":null}'

# 1-shot measurement sequence (write → wait → read)
curl -s -X POST http://localhost:8000/api/mcp/tool-call \
  -H "Content-Type: application/json" \
  -d '{"server":"saiverse-stackchan-addon__stackchan","tool_name":"i2c_write","arguments":{"addr":<ADDR>,"bytes":[<MEASURE_CMD>]},"persona_id":null}'
sleep 0.02
curl -s -X POST http://localhost:8000/api/mcp/tool-call \
  -H "Content-Type: application/json" \
  -d '{"server":"saiverse-stackchan-addon__stackchan","tool_name":"i2c_read","arguments":{"addr":<ADDR>,"n_bytes":<N>},"persona_id":null}'
```

補正計算 (calibration register parse + Q-format 演算など) が要る Unit は、 取得した raw bytes を `temp/<unit>_inline_test.py` で Python inline 実行 → 値が reasonable か確認 → 同じロジックを `tools/units/<unit>.py` に移植、 の順で進めると compensation の bug を踏みません。 `env3.py` の QMP6988 実装 + `temp/qmp6988_inline_test.py` (検証時に作成) が参考例。

### Step 4. 動作確認 (= SAIVerse 再起動 → toggle ON → ペルソナ)

1. SAIVerse 再起動 → `tools/__init__.py:_autodiscover_tools()` が新 file を picked up
2. backend.log で `Registered tool from .../<unit>.py (addon=saiverse-stackchan-addon)` が出ているか確認
3. AddonManager UI で「<Display Name> を有効化」 toggle を ON
   - toggle ON で AddonConfig が更新 + `tools/mcp_client.py:reconnect_server` で MCP subprocess が新 env で再起動
   - native tool の `schemas()` は spell surface 構築のたびに呼ばれるので、 reconnect 後に即時 spell visibility が反映される
4. Vessel Building にペルソナを配置、 spell の description に該当する話題を振る (= 「温度教えて」 等)
5. spell 発動 → 想定値が返答に出るか確認。 失敗時は backend.log の `[sea][spell] Executed get_<unit>_<m> →` 行で実際の戻り値を確認

## Pitfalls (既知の落とし穴)

### `i2c_write_read` 1 発では measurement wait できない

`i2c_write_read` は write + Repeated Start + read を 1 transaction で行うため、 master 側で wait を挟めません。 SHT30 高精度モード (`0x2C06`、 ~15 ms measurement time) のような **時間が要る測定** はこれだと read 部分で 0xFF sentinel もしくは driver error が返ります。

→ **`i2c_write` → `asyncio.sleep(必要 ms)` → `i2c_read`** の 3 段に分ける必要があります。

```python
# NG (15 ms wait できない、 0xFF 0xFF 0xFF が返る):
await conn.call_tool("i2c_write_read",
    {"addr": 0x44, "write_bytes": [0x2C, 0x06], "n_bytes": 6})

# OK:
await conn.call_tool("i2c_write", {"addr": 0x44, "bytes": [0x2C, 0x06]})
await asyncio.sleep(0.020)
await conn.call_tool("i2c_read", {"addr": 0x44, "n_bytes": 6})
```

例外: 「register pointer + read」 (= measurement trigger ではなく単純な register 読み出し) なら `i2c_write_read` 1 発で OK。 例: QMP6988 の chip ID register 0xD1 や、 SHT30 の status register 0xF32D。

### 遅い Unit は 400 kHz だと `ESP_ERR_INVALID_STATE` で落ちる

Port A 汎用 i2c tool (`i2c_read` / `i2c_write` / `i2c_write_read`) は I2C クロックを既定 **400 kHz** で叩きます。 これに追従できない遅い Unit は **`i2c_scan` (probe) では ACK を返すのに、 実際の `i2c_write` / `i2c_read` の transmit が `ESP_ERR_INVALID_STATE` (= esp_err 0x103) で毎回失敗** します (= ファームの PY32 IO expander が 100 kHz で踏んだのと同じ罠。 stackchan.cc の該当コメント参照)。

代表例が **RCWL-9620 超音波測距ユニット** (`sonic.py`)。 各 i2c 呼び出しの args に **`scl_speed_hz`** を渡してクロックを下げます (firmware の optional property、 既定 400000、 range 100000〜1000000)。

**write と read で必要な速度が違う (read のほうがシビア)**。 RCWL-9620 の実機検証 (PaHUB ch5 単独 / 全 channel 開放いずれでも同じ) で判明:

| 操作 | 駆動側 | 200 kHz | 100 kHz |
|---|---|---|---|
| trigger `i2c_write` | master が SDA 駆動 | ACK する | OK |
| 測定値 `i2c_read` | slave が SDA 駆動 (ESP-IDF `i2c_master_receive` がサンプリング) | `ESP_ERR_INVALID_STATE` / 不定値で erratic | 安定 (5/5 同値) |

master がバスを駆動する write は速度耐性が高く、 slave が駆動して master が読む read はタイミング/信号品質に敏感で律速になります。 **read が通る速度に両方を合わせる** (RCWL-9620 では 100 kHz) のが安全。

**裏付け**: RCWL-9620 の I2C 上限速度は datasheet に明記が無く (チップ自体マイナーで仕様が薄い)、 M5 公式材料も一貫しません — `m5stack/M5Unit-Sonic` の `Unit_Sonic.h` は `begin(..., speed=200000L)` (200 kHz) を既定にする一方、 一部 example は `400000U` (400 kHz) を渡しています。 つまり「100 kHz でないとダメ」 と公式に書いてある一次情報は無い。 ただし M5 コミュニティで同型ユニットの同じ症状が独立に報告されており (高速だと「毎サンプルの半分が 4500 mm を返す」 = 本実装で 300 kHz 時に観測した `0xFFFFFF`→クランプと一致)、 解決策が `"if I set the i2c rate to 100Khz this works"` と確認されています ([M5Stack Community: Ultrasonic I2C problems](https://community.m5stack.com/topic/4255/ultrasonic-i2c-problems/2))。 本実装の 100 kHz はこの実測 + コミュニティ報告に基づく値で、 write/read の非対称自体は ESP-IDF `i2c_master_receive` + PaHUB 経由での挙動 (datasheet には載らない実装側の事実)。

```python
# 遅い Unit: read が律速。 両方 100 kHz に下げる
await conn.call_tool("i2c_write",
    {"addr": 0x57, "bytes": [0x01], "scl_speed_hz": 100000})
await asyncio.sleep(0.120)
await conn.call_tool("i2c_read",
    {"addr": 0x57, "n_bytes": 3, "scl_speed_hz": 100000})
```

`scl_speed_hz` property は firmware 側 (`temp/stackchan-mcp` の `boards/stackchan/stackchan.cc`、 `self.i2c.read/write/write_read` の 3 tool) に実装されています。 **未対応の旧 firmware では未知 arg として無視される**ので、 古いファームのまま動かすと 400 kHz のままで遅い Unit は動きません (= firmware 更新 + flash が前提)。 症状の切り分けには `i2c_scan` を使い、 「scan には出るが read/write が INVALID_STATE」 なら速度問題と判断できます。

### 戻り値型は `str` か `(str, dict)`、 4-tuple は NG

SAIVerse の SEA runtime は **`str` または `(str, dict)` の 2-tuple** しか正規対応していません。 4-tuple `(text, ToolResult, file_path, metadata)` を return すると tuple 全体が `str()` 化されて LLM に repr 文字列がそのまま渡ります (= 既存 `see.py` も同じ bug を抱えている、 詳細は `docs/issues/native_tool_return_4tuple_bug.md`)。

attachment が要らない測定系 spell は `str` 一択。

### 文体は丁寧語 + 客観的事実 (キャラ付けない)

ペルソナ向け text にキャラ付け (口語 / 親しみ調 / 「だった」 「だね」 / 感嘆符多用) を盛らないこと。 ペルソナっぽい言葉を話すのはペルソナ自身の仕事で、 tool 側がそこに踏み込むと「ペルソナが自分の言葉で語り直す」 余地が狭まります。

```
❌ 「いま測ってきた。 温度は 32.3°C、 湿度は 46.4% だった!」
✅ 「温度: 32.3°C、湿度: 46.4% (ENV III / SHT30、Stack-chan Port A)。」
```

エラーメッセージも同じく丁寧語 + 客観で。 「〜してほしい」 ではなく「〜してください」。

### Clock stretching な cmd を読み違えない

SHT30 の例: `0x2C06` (高精度 + clock stretching **enabled**、 measurement 中は SCL pull down で wait) と `0x2400` (高精度 + clock stretching **disabled**、 measurement 中に read すると NACK) は挙動が違います。 datasheet で「Clock Stretching」 の有無を確認してから cmd を選んでください。

### Cache すべき固定値は module-level に持つ

QMP6988 の calibration coefficients のように **device 再起動まで変化しない 24+ byte の OTP 値** は、 spell 呼び出しごとに読み直すと I2C 通信時間が無駄になります。 `env3.py` の `_qmp6988_ik_cache` 方式 (= module-level Optional[dict]、 初回呼び出しで read してキャッシュ) を踏襲してください。

## チェックリスト

新 Unit driver 完成までの確認項目:

- [ ] `addon.json` に `unit_<name>_enabled` toggle を追加
- [ ] `tools/units/<unit>.py` を作成、 `schemas()` で spell list を return
- [ ] 戻り値型は `str` (4-tuple は使わない)
- [ ] エラー文を含めて全 text は丁寧語 + 客観表現
- [ ] `ruff check expansion_data/saiverse-stackchan-addon/tools/units/<unit>.py` passed
- [ ] curl 直叩きで chip ID / measurement の正常動作確認 (= 仮説検証)
- [ ] (補正計算が要る Unit) Python inline で raw → 物理単位の換算が reasonable と確認
- [ ] SAIVerse 再起動 → backend.log に `Registered tool from .../<unit>.py` が出る
- [ ] AddonManager UI で `unit_<name>_enabled` を ON にして spell が persona に visible になる
- [ ] ペルソナ会話で spell 発動 + 想定値が返答に含まれる

## 参考実装

- **`sonic.py`** — M5Stack 超音波測距ユニット I2C (RCWL-9620、 0x57)。 「write (測距トリガ) → 120 ms wait → 3 byte read → 24-bit raw を /1000 で mm 換算」 だけの**最小サンプル** (CRC も calibration も無し)。 新規 Unit がこの単純パターンに収まるなら sonic.py を雛形にすると速い
- **`env3.py`** — M5Stack ENV III (温湿度: SHT30 0x44 + 気圧: QMP6988 0x70)。 1 file で「clock stretching enable cmd + 単純 measurement」 (SHT30) と「OTP calibration register 読み出し + cache + Q-format compensation」 (QMP6988) の両パターンを実装。 包括的なサンプル
- **`servo8.py`** — M5Stack 8Servos Unit (U165、 0x25)。 read を伴わない **write-only 制御系** + 「MODE → 値の 2 段書き込み」 + 引数付き spell (channel / angle / speed) のサンプル

## 関連 doc

- `docs/intent/stackchan_extension_modules.md` (SAIVerse 本体 repo) — 拡張モジュール対応の設計指針 (C 案 = 汎用口 + 個別 Unit プリセット + addon ドライバの 3 段階モデル)
- `docs/issues/stackchan_mcp_i2c_generic_tools.md` (SAIVerse 本体 repo) — PR ② の設計確定事項 + 動作確認結果
- `docs/issues/native_tool_return_4tuple_bug.md` (SAIVerse 本体 repo) — 4-tuple 戻り値 bug 詳細 + 修正方針案
- M5Stack 公式 Unit カタログ: <https://docs.m5stack.com/en/products/unit>
- Sensirion datasheet: <https://sensirion.com/products/catalog/> (SHT3x など)
- QST QMP6988 datasheet: M5Stack OSS mirror に PDF あり
