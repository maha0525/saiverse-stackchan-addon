"""デバイス操作スペル群 ― 表情 / 口 / LED / 画面輝度 / 音量。

生 MCP ツール (``set_avatar`` / ``set_mouth`` / ``set_led`` …、 stackchan-mcp
gateway 経由) をペルソナに直接見せず、 現在ペルソナが降りている機体の gateway
インスタンスへ転送する薄い native tool 群 (intent stackchan_vessel.md 設計
K-4)。生ツールを ``mcp_servers.json`` で ``visible:false`` にして隠し、 ペルソナ
には機体に依らない単一論理名だけを見せる (move_head / see / body_status と同じ
構図)。複数機体の同時稼働では ``resolve_vessel_connection`` が「いまその身体が
降りている機体」へ各コールを振り分ける。

これらは全 Stack-chan が必ず持つ共通デバイス (画面 / スピーカー / LED) の操作な
ので、 building_ids は全 Vessel Building (``list_vessel_building_ids``)。ユニット
由来ツール (env3 等) の capability ゲートとは異なり、 機体を選ばない。

1 ファイル複数 spell 登録: SAIVerse の tool loader
(``tools/__init__.py:_register_multiple_tools``) は module が ``schemas()`` を
持てば優先で呼び、 ``ToolSchema.name`` と同名の module 関数を実装として束ねる
(``tools/units/env3.py`` の ``schemas()`` パターンを踏襲)。

引数スキーマは gateway (``stackchan_mcp/stdio_server.py`` の Tool 定義) と 1:1 で
一致させている (enum / 範囲 / ネスト構造)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, List

from tools.core import ToolSchema

# addon root (vessel_dispatch.py と同階層) を import 可能にする。 tool loader は
# addon の tools/ までしか sys.path に積まないため 1 段上を明示的に通す
# (move_head.py が vessel_dispatch を import するのと同じ事情)。
_ADDON_ROOT = str(Path(__file__).resolve().parent.parent)
if _ADDON_ROOT not in sys.path:
    sys.path.insert(0, _ADDON_ROOT)
from vessel_dispatch import (  # noqa: E402
    VesselNotAvailable,
    building_gate_or_hidden,
    list_vessel_building_ids,
    resolve_vessel_connection,
)

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"

_DEFAULT_TIMEOUT_SEC = 10.0

_NO_VESSEL_MSG = (
    "この操作は Stack-chan の身体に降りているときだけ行えます。"
    " 現在は身体に降りていないか、 機体が接続されていません。"
)


# ============================================================
# 共通ヘルパ (生 MCP デバイスツールを現在機体へ転送)
# ============================================================

async def _call_raw(raw_name: str, args: dict) -> str:
    """現在ペルソナが降りている機体の gateway で生 MCP ツールを呼ぶ。"""
    _vessel, conn = resolve_vessel_connection()
    return await conn.call_tool(raw_name, args)


def _run_on_mcp_loop(coro, timeout_sec: float = _DEFAULT_TIMEOUT_SEC) -> str:
    """sync な native tool から MCP client の event loop へ橋渡しする。"""
    import tools.mcp_client as _mcp

    loop = _mcp._loop
    if loop is None:
        # 未スケジュールの coroutine を閉じて "never awaited" 警告を防ぐ。この分岐は
        # MCP 未起動時のみ通る (schedule 後は loop 所有なので coro には触らない)。
        coro.close()
        raise RuntimeError("MCP event loop is not initialized")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout_sec)


def _invoke(raw_name: str, args: dict, success_msg: str, fail_prefix: str) -> str:
    """生 MCP デバイスツールを現在機体へ転送し、 客観テキストを返す。

    生ツールはエラー時 ``{"error": "..."}`` 等を返すので、 それを拾って失敗
    メッセージに変換する。 成功時は呼び出し側が渡した ``success_msg`` を返す。
    """
    try:
        rendered = _run_on_mcp_loop(_call_raw(raw_name, args))
    except VesselNotAvailable:
        return _NO_VESSEL_MSG
    except Exception as exc:  # noqa: BLE001 - user-facing tool は握って客観返す
        LOGGER.exception("%s: MCP call failed", raw_name)
        return f"{fail_prefix}: {exc}"

    try:
        payload = json.loads(rendered)
    except (json.JSONDecodeError, TypeError):
        payload = None
    if isinstance(payload, dict) and payload.get("error"):
        LOGGER.warning("%s: device returned error: %s", raw_name, payload["error"])
        return f"{fail_prefix}: {payload['error']}"

    LOGGER.info("%s: ok (args=%s)", raw_name, args)
    return success_msg


# ============================================================
# Tool implementations (関数名は schema.name と一致させる)
# ============================================================

def set_avatar(face: str) -> str:
    """LCD に表示する表情を切り替える。"""
    return _invoke(
        "set_avatar", {"face": face},
        f"表情を「{face}」に切り替えました。",
        "表情を切り替えられませんでした",
    )


def set_mouth(mouth: str) -> str:
    """口の形をリップシンク用に設定する。"""
    return _invoke(
        "set_mouth", {"mouth": mouth},
        f"口の形を「{mouth}」に設定しました。",
        "口の形を設定できませんでした",
    )


def set_mouth_sequence(steps: List[dict]) -> str:
    """口パクのシーケンスをまとめて再生する。"""
    n = len(steps) if isinstance(steps, list) else 0
    return _invoke(
        "set_mouth_sequence", {"steps": steps},
        f"口パクシーケンス（{n} ステップ）を再生しました。",
        "口パクシーケンスを再生できませんでした",
    )


def set_led(index: int, r: int, g: int, b: int) -> str:
    """台座の RGB LED を 1 個指定して色を変える (index 0..11)。"""
    return _invoke(
        "set_led", {"index": index, "r": r, "g": g, "b": b},
        f"LED {index} を RGB({r}, {g}, {b}) に設定しました。",
        "LED を設定できませんでした",
    )


def set_all_leds(r: int, g: int, b: int) -> str:
    """台座の 12 個の RGB LED を全部同じ色にする。"""
    return _invoke(
        "set_all_leds", {"r": r, "g": g, "b": b},
        f"全 LED を RGB({r}, {g}, {b}) に設定しました。",
        "LED を設定できませんでした",
    )


def set_leds(colors: List[List[int]]) -> str:
    """複数の RGB LED をまとめて設定する ([r,g,b] の配列)。"""
    return _invoke(
        "set_leds", {"colors": colors},
        "複数の LED を設定しました。",
        "LED を設定できませんでした",
    )


def clear_leds() -> str:
    """台座の 12 個の RGB LED を全消灯する。"""
    return _invoke(
        "clear_leds", {},
        "LED を全消灯しました。",
        "LED を消灯できませんでした",
    )


def set_brightness(brightness: int) -> str:
    """画面の明るさを 0..100 で設定する。"""
    return _invoke(
        "set_brightness", {"brightness": brightness},
        f"画面の明るさを {brightness} に設定しました。",
        "画面の明るさを設定できませんでした",
    )


def set_volume(volume: int) -> str:
    """スピーカー音量を 0..100 で設定する。"""
    return _invoke(
        "set_volume", {"volume": volume},
        f"音量を {volume} に設定しました。",
        "音量を設定できませんでした",
    )


# ============================================================
# Spell registry
# ============================================================

_RGB_PROP = {
    "r": {"type": "integer", "description": "赤 0..255", "minimum": 0, "maximum": 255},
    "g": {"type": "integer", "description": "緑 0..255", "minimum": 0, "maximum": 255},
    "b": {"type": "integer", "description": "青 0..255", "minimum": 0, "maximum": 255},
}


def schemas() -> List[ToolSchema]:
    """デバイス操作スペル 9 個を一括登録する。

    共通デバイス操作なので building_ids は全 Vessel Building
    (``list_vessel_building_ids``)。 機体未登録なら building_gate_or_hidden が
    センチネル + ``spell_visible=False`` に倒して隠す (= どこにも出さない・実行
    不可、 安全側)。 spell surface 構築のたびに呼ばれるので、 ペアリング追加後の
    reconnect で即 visible になる。
    """
    building_ids, visible = building_gate_or_hidden(list_vessel_building_ids())
    if not visible:
        LOGGER.info(
            "device_controls: no vessel registered yet; spells hidden everywhere until pairing."
        )

    def mk(
        name: str,
        description: str,
        display_name: str,
        properties: dict,
        required: list,
    ) -> ToolSchema:
        return ToolSchema(
            name=name,
            description=description,
            parameters={
                "type": "object",
                "properties": properties,
                "required": required,
            },
            result_type="string",
            spell=True,
            spell_display_name=display_name,
            spell_visible=visible,
            building_ids=building_ids,
        )

    return [
        mk(
            "set_avatar",
            (
                "あなたの身体 (Stack-chan) の LCD に表示する表情を切り替える。"
                " これは単なるラベルではなく、 実際に画面に見える顔が変わる。"
                " 'off' を渡すと表情を隠して下の設定画面 (WiFi 設定等) を出す。"
            ),
            "表情を変える",
            {
                "face": {
                    "type": "string",
                    "enum": [
                        "idle", "happy", "thinking", "sad",
                        "surprised", "embarrassed", "off",
                    ],
                    "description": (
                        "表情。 idle / happy / thinking / sad / surprised /"
                        " embarrassed / off のいずれか。"
                    ),
                },
            },
            ["face"],
        ),
        mk(
            "set_mouth",
            (
                "あなたの身体 (Stack-chan) の口の形をリップシンク用に設定する。"
                " 次の set_avatar / set_mouth 呼び出しまで、 もしくは自動まばたきが"
                " 素の顔に戻すまで保持される。"
            ),
            "口形状を設定",
            {
                "mouth": {
                    "type": "string",
                    "enum": ["closed", "half", "open", "e", "u"],
                    "description": "口の形。 closed / half / open / e / u のいずれか。",
                },
            },
            ["mouth"],
        ),
        mk(
            "set_mouth_sequence",
            (
                "口パクのシーケンスをまとめて再生する。 各ステップは shape を"
                " duration_ms ミリ秒保持してから次へ進む。 device 側でキューを"
                " 歩進するので、 set_mouth を連発するより滑らか。 呼ぶと即座に"
                " 返り、 再生は device 側で進む。"
            ),
            "口パクシーケンス",
            {
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 256,
                    "items": {
                        "type": "object",
                        "properties": {
                            "shape": {
                                "type": "string",
                                "enum": ["closed", "half", "open", "e", "u"],
                                "description": (
                                    "このステップの口の形。 closed / half /"
                                    " open / e / u のいずれか。"
                                ),
                            },
                            "duration_ms": {
                                "type": "integer",
                                "minimum": 10,
                                "maximum": 10000,
                                "description": "この形を保持する時間 (ミリ秒、 10..10000)。",
                            },
                        },
                        "required": ["shape", "duration_ms"],
                    },
                    "description": "口の形と保持時間の順序付きリスト (1..256 ステップ)。",
                },
            },
            ["steps"],
        ),
        mk(
            "set_led",
            (
                "あなたの身体 (Stack-chan) の台座 RGB LED を 1 個指定して色を"
                " 変える。 LED は 2 行 6 列の計 12 個 (index 0..11)。"
            ),
            "LED を変える",
            {
                "index": {
                    "type": "integer",
                    "description": "LED の位置 (0..11)。",
                    "minimum": 0,
                    "maximum": 11,
                },
                **_RGB_PROP,
            },
            ["index", "r", "g", "b"],
        ),
        mk(
            "set_all_leds",
            "台座の 12 個の RGB LED を全部同じ色にする。",
            "全 LED を変える",
            dict(_RGB_PROP),
            ["r", "g", "b"],
        ),
        mk(
            "set_leds",
            (
                "複数の RGB LED をまとめて設定する。 colors は [r,g,b] の三つ組の"
                " 配列で、 index 0 から順に対応する (最大 12 個)。 アニメーションや"
                " パターン表示向け。"
            ),
            "複数 LED を変える",
            {
                "colors": {
                    "type": "array",
                    "description": "[r,g,b] 三つ組の配列 (各値 0..255)。",
                    "items": {
                        "type": "array",
                        "items": {"type": "integer", "minimum": 0, "maximum": 255},
                        "minItems": 3,
                        "maxItems": 3,
                    },
                    "minItems": 1,
                    "maxItems": 12,
                },
            },
            ["colors"],
        ),
        mk(
            "clear_leds",
            "台座の 12 個の RGB LED を全消灯する。",
            "LED 消灯",
            {},
            [],
        ),
        mk(
            "set_brightness",
            "あなたの身体 (Stack-chan) の画面の明るさを 0 (暗) 〜 100 (明) で設定する。",
            "画面輝度",
            {
                "brightness": {
                    "type": "integer",
                    "description": "明るさ (0..100)。",
                    "minimum": 0,
                    "maximum": 100,
                },
            },
            ["brightness"],
        ),
        mk(
            "set_volume",
            (
                "あなたの身体 (Stack-chan) のスピーカー音量を 0 (無音) 〜 100 (最大)"
                " で設定する。 発話が大きすぎ / 小さすぎるときに自分で調整できる。"
            ),
            "音量設定",
            {
                "volume": {
                    "type": "integer",
                    "description": "音量 (0..100)。",
                    "minimum": 0,
                    "maximum": 100,
                },
            },
            ["volume"],
        ),
    ]
