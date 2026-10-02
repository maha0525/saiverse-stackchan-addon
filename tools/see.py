"""``see`` ― Vessel Building 内のペルソナが Stack-chan の目で世界を見る。

生 MCP ツール ``take_photo`` (stackchan-mcp gateway 経由) をラップして、
撮影された JPEG を SAIVerse 媒体ストアにコピーし、 LLM の attachment
経路に乗せる。 ペルソナは ``image_path`` 文字列ではなく実際の画像を
受け取れるので、 自分の目で見たという認知モデルが成立する。

長期設計 (汎用 MediaBuffer + promote_media) は
``docs/intent/multimodal_input_pipeline.md`` 参照。 本ツールは画像返却
MCP サーバーがまだ stackchan しかない段階での個別解として、 既存
``image_generator.py`` の attachment 経路に直接乗せる薄い実装。
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from tools.core import ToolResult, ToolSchema
from saiverse.media_utils import store_image_bytes

# addon root (vessel_dispatch.py) を import 可能にする。詳細は move_head.py の
# 同コメント参照。
_ADDON_ROOT = str(Path(__file__).resolve().parent.parent)
if _ADDON_ROOT not in sys.path:
    sys.path.insert(0, _ADDON_ROOT)
from vessel_dispatch import (  # noqa: E402
    building_gate_or_hidden,
    list_vessel_building_ids,
    resolve_vessel_connection,
)

LOGGER = logging.getLogger(__name__)

ADDON_NAME = "saiverse-stackchan-addon"
MCP_QUALIFIED_SERVER = f"{ADDON_NAME}__stackchan"
MCP_TOOL_TAKE_PHOTO = "take_photo"

_DEFAULT_TIMEOUT_SEC = 30.0


async def _call_take_photo(question: str) -> str:
    """Call the raw ``take_photo`` MCP tool and return its rendered string."""
    _vessel, conn = resolve_vessel_connection()
    return await conn.call_tool(MCP_TOOL_TAKE_PHOTO, {"question": question})


def _run_on_mcp_loop(coro, timeout_sec: float = _DEFAULT_TIMEOUT_SEC) -> str:
    """Bridge from a sync tool to the MCP client's event loop."""
    import tools.mcp_client as _mcp

    loop = _mcp._loop
    if loop is None:
        # 未スケジュールの coroutine を閉じて "never awaited" 警告を防ぐ。この分岐は
        # MCP 未起動時のみ通る (schedule 後は loop 所有なので coro には触らない)。
        coro.close()
        raise RuntimeError("MCP event loop is not initialized")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout_sec)


def _error_return(message: str) -> Tuple[str, ToolResult, None, None]:
    return message, ToolResult(history_snippet=None), None, None


def see(
    question: str = "",
) -> Tuple[str, ToolResult, Optional[str], Optional[Dict[str, Any]]]:
    """Stack-chan の目で見た景色を画像 attachment として返す。

    Args:
        question: 視覚で確認したいことの問い (任意)。 戻り値 text に
            メモとして埋まる。

    Returns:
        ``(text, ToolResult, file_path, metadata)`` 4 要素タプル。
        ``metadata['media']`` に画像 descriptor を 1 件含むので、
        LLM の attachment 経路で実画像が届く。
    """
    try:
        rendered = _run_on_mcp_loop(_call_take_photo(question))
    except Exception as exc:
        LOGGER.exception("see: take_photo MCP call failed")
        return _error_return(f"カメラから画像を取れなかった: {exc}")

    # gateway の capture_server.py が返す JSON ペイロードを期待する。
    # 想定形式: {"image_path": "...", "size_bytes": N, "question": "..."}
    # IMU ブランチ以降の gateway は take_photo の返答に画像本体も同梱する
    # (JSON テキスト + image ブロックの 2 部構成)。mcp_client 側の文字列化で
    # 画像は "[binary: N bytes]" の行になるため、行ごとに走査して最初に
    # JSON として読める行を採用する。実画像は image_path から自前で読む
    # (下の store_image_bytes 経路) ので、同梱データ自体は使わない。
    payload = None
    for line in (rendered or "").splitlines():
        try:
            parsed = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break
    if payload is None:
        LOGGER.warning("see: take_photo returned non-JSON: %r", rendered)
        return _error_return(f"カメラの返答を解釈できなかった: {rendered}")

    if "error" in payload:
        LOGGER.warning("see: take_photo returned error: %s", payload["error"])
        return _error_return(f"カメラからエラー: {payload['error']}")

    image_path_str = payload.get("image_path")
    if not image_path_str:
        LOGGER.warning("see: take_photo payload has no image_path: %r", payload)
        return _error_return("カメラから画像のパスが返ってこなかった")

    src_path = Path(image_path_str)
    if not src_path.exists():
        LOGGER.warning("see: image file not found: %s", src_path)
        return _error_return(f"画像ファイルが見当たらない: {src_path}")

    try:
        image_bytes = src_path.read_bytes()
    except OSError as exc:
        LOGGER.exception("see: failed to read image bytes")
        return _error_return(f"画像ファイルを読めなかった: {exc}")

    mime_type = mimetypes.guess_type(str(src_path))[0] or "image/jpeg"

    try:
        metadata_entry, stored_path = store_image_bytes(
            image_bytes, mime_type, source="tool:see"
        )
    except Exception as exc:
        LOGGER.exception("see: failed to store image into SAIVerse media store")
        return _error_return(f"画像を媒体ストアに保存できなかった: {exc}")

    metadata = {"media": [metadata_entry]}
    snippet = f"![見えた光景]({stored_path.as_posix()})"
    text = "目の前の光景を見た。"
    if question:
        text += f" (問い: {question})"

    LOGGER.info(
        "see: captured image stored=%s mime=%s size=%d",
        stored_path,
        mime_type,
        len(image_bytes),
    )
    return text, ToolResult(history_snippet=snippet), stored_path.as_posix(), metadata


def schema() -> ToolSchema:
    # 共通身体ツールは全 Vessel Building で visible (intent 不変条件 #14
    # 共通ツール側)。機体未登録なら building_gate_or_hidden がセンチネル +
    # spell_visible=False に倒し、全 Building で非表示・実行不可にする。
    building_ids, visible = building_gate_or_hidden(list_vessel_building_ids())
    if not visible:
        LOGGER.info(
            "see: no vessel registered yet; tool hidden everywhere until pairing."
        )
    return ToolSchema(
        name="see",
        description=(
            "あなたの目で目の前の光景を見る。 視覚で何かを確認したいときに呼ぶ。"
            " 戻り値には実際に見えた景色が画像として添付される。"
            " 問いを添えると注目したい点をメモとして残せる (任意)。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "見ながら確認したいことや注目したい点 (任意)",
                },
            },
            "required": [],
        },
        result_type="string",
        spell=True,
        spell_display_name="見る",
        spell_visible=visible,
        building_ids=building_ids,
    )
