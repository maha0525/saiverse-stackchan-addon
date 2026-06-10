"""Stack-chan Vessel: avatar セット最終化パイプライン (Phase 4.5-d-3)。

段階 ④ (一括トリミング、 stage_executor 経由) → ⑤ (リサイズ + RGB565
変換 + avatar.bin 連結、 別 endpoint) → ⑥ (Stack-chan へ転送、 別 endpoint)
を実装。

出力先は既存 avatar_loader.py が読む path に合流:
  <set_dir>/avatar.bin     ← RGB565 raw 連結 (537,600 or 3,456,000 bytes)
  <set_dir>/manifest.json  ← {"mode": ..., "checksum": "sha256:..."}

詳細: docs/intent/stackchan_avatar_pipeline.md §D-4 (④⑤⑥)
"""
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

_PACK_DIR = str(Path(__file__).parent)
if _PACK_DIR not in sys.path:
    sys.path.insert(0, _PACK_DIR)

import avatar_pipeline as ap  # noqa: E402

LOGGER = logging.getLogger(__name__)

# 既存 firmware avatar_set / convert_avatars.py と整合する constants。
TARGET_W = 160
TARGET_H = 120
FRAME_BYTES = TARGET_W * TARGET_H * 2  # 38,400 bytes per frame in RGB565

# Layered mode の出力 ordering (= firmware の AvatarSet と一致)。
LAYERED_TOTAL_FRAMES = (
    len(ap.FACE_NAMES) + len(ap.EYES_STATES) + len(ap.MOUTH_SHAPES)
)  # 6 + 3 + 5 = 14
LAYERED_TOTAL_BYTES = LAYERED_TOTAL_FRAMES * FRAME_BYTES  # 537,600
MATRIX_TOTAL_BYTES = ap.MATRIX_TOTAL_FRAMES * FRAME_BYTES  # 3,456,000


def _pil_image():
    """Pillow Image を import (= 起動時に依存 install 必須を強制したくない)。"""
    from PIL import Image
    return Image


# ----- Stage 04: Trim (= stage_executor 経由で呼ばれる) -----


def _trim_one(src: Path, dst: Path, rect: dict[str, int]) -> None:
    Image = _pil_image()
    with Image.open(src) as im:
        im = im.convert("RGB")
        x = int(rect["x"])
        y = int(rect["y"])
        w = int(rect["width"])
        h = int(rect["height"])
        # rect に「編集時に表示していた画像の natural size」 (ref_width /
        # ref_height) が入っている場合、 適用先画像のサイズが違えば比例
        # スケールして適用する。 ① 手動アップロード由来の face.png (= 任意
        # の crop 実寸) と ②③ 生成画像 (= backend 固定サイズ、 例 gpt 4:3
        # = 1536×1152) はピクセルサイズが違うため、 絶対座標のままでは
        # 同 face 内で切り出し位置がズレる。 同アス比前提で相対適用する。
        ref_w = int(rect.get("ref_width") or 0)
        ref_h = int(rect.get("ref_height") or 0)
        img_w, img_h = im.size
        if ref_w > 0 and ref_h > 0 and (ref_w, ref_h) != (img_w, img_h):
            sx = img_w / ref_w
            sy = img_h / ref_h
            LOGGER.debug(
                "avatar_finalizer: _trim_one scaling rect %s for %s "
                "(ref=%dx%d -> img=%dx%d)",
                rect, src.name, ref_w, ref_h, img_w, img_h,
            )
            x = int(round(x * sx))
            y = int(round(y * sy))
            w = int(round(w * sx))
            h = int(round(h * sy))
        # スケール丸めで 1px はみ出すケースの保険 (= PIL の crop は範囲外を
        # 黒 pad で握り潰すので、 はみ出しはここで clamp して検出可能にする)。
        x = max(0, min(x, img_w - 1))
        y = max(0, min(y, img_h - 1))
        w = max(1, min(w, img_w - x))
        h = max(1, min(h, img_h - y))
        crop = im.crop((x, y, x + w, y + h))
    dst.parent.mkdir(parents=True, exist_ok=True)
    crop.save(dst, format="PNG")


def _collect_trim_inputs(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
    mode: str,
) -> list[tuple[str, Path]]:
    """mode に応じてトリミング対象ファイルを (target_name, src_path) リストで返す。

    出力名規約:
      matrix : `{face}_{eyes}_{mouth}` (90 個)
      layered: `face_{name}` (6), `eyes_{state}` (3), `mouth_{shape}` (5) = 14 個

    layered の face_idle は ① の 01_face/face.png を流用、 残り 5 表情は ②。
    """
    inputs: list[tuple[str, Path]] = []
    if mode == "matrix":
        matrix_dir = mgr.stage_dir(persona_id, set_name, ap.STAGE_MATRIX)
        for face in ap.FACE_NAMES:
            for eyes in ap.EYES_STATES:
                for mouth in ap.MOUTH_SHAPES:
                    name = f"{face}_{eyes}_{mouth}"
                    src = matrix_dir / f"{name}.png"
                    if not src.exists():
                        raise FileNotFoundError(
                            f"matrix mode input not found: {src}"
                        )
                    inputs.append((name, src))
    elif mode == "layered":
        face_path = (
            mgr.stage_dir(persona_id, set_name, ap.STAGE_FACE) / "face.png"
        )
        if not face_path.exists():
            raise FileNotFoundError(f"01_face/face.png not found: {face_path}")
        inputs.append(("face_idle", face_path))
        for expr in ap.EXPRESSION_NAMES:
            src = (
                mgr.stage_dir(persona_id, set_name, ap.STAGE_EXPRESSIONS)
                / f"{expr}.png"
            )
            if not src.exists():
                raise FileNotFoundError(
                    f"02_expressions/{expr}.png not found: {src}"
                )
            inputs.append((f"face_{expr}", src))
        layered_dir = mgr.stage_dir(persona_id, set_name, ap.STAGE_LAYERED)
        for eyes in ap.EYES_STATES:
            src = layered_dir / f"eyes_{eyes}.png"
            if not src.exists():
                raise FileNotFoundError(
                    f"03_layered/eyes_{eyes}.png not found: {src}"
                )
            inputs.append((f"eyes_{eyes}", src))
        for mouth in ap.MOUTH_SHAPES:
            src = layered_dir / f"mouth_{mouth}.png"
            if not src.exists():
                raise FileNotFoundError(
                    f"03_layered/mouth_{mouth}.png not found: {src}"
                )
            inputs.append((f"mouth_{mouth}", src))
    else:
        raise ValueError(f"Unknown mode: {mode!r}")
    return inputs


def _variant_key_for_target(target: str, mode: str) -> str:
    """trim_rect_overrides の key を target から逆引きする。

    matrix mode: target = "{face}_{eyes}_{mouth}" → face 名 (= 同 face の
                 15 セルは同じ rect、 まはー検証 2026-05-17)
    layered mode: target = "face_<n>" / "eyes_<s>" / "mouth_<m>" → そのまま
                  (= 14 個別々の rect 持ち得る)
    """
    if mode == "matrix":
        parts = target.split("_")
        if parts:
            return parts[0]
        return target
    return target


def generate_stage_trim(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """④ 一括トリミング。 trim_rect を全画像に適用 → WIP 04_trimmed/ に保存。

    rect 解決ロジック (= まはー検証 2026-05-17):
      matrix : meta.trim_rect_overrides[face] があれば それ → なければ default
      layered: meta.trim_rect_overrides[target] があれば それ → なければ default
      default が無ければエラー (= overrides 単独では動かない、 default は必須)

    params:
      trim_rect: optional, metadata default を上書き (= UI から最新 rect)
      only_target: optional, この target だけ trim する (= 単発 trim、 ④編集中)
      face_filter: optional, matrix で同 face の 14-15 セルだけ trim する
    """
    meta = mgr.read_metadata(persona_id, set_name)
    if meta is None:
        raise FileNotFoundError(f"WIP not found: {persona_id}/{set_name}")
    default_rect = params.get("trim_rect") or meta.trim_rect
    overrides = meta.trim_rect_overrides or {}
    if not default_rect:
        raise ValueError(
            "trim_rect is required: set via PATCH metadata first, or pass "
            "in params"
        )

    inputs = _collect_trim_inputs(mgr, persona_id, set_name, meta.mode)

    # 単発 trim 用フィルタ (= ④ で 1 枚だけ rect 試したい時)。
    only_target = params.get("only_target")
    if isinstance(only_target, str) and only_target:
        inputs = [(name, src) for (name, src) in inputs if name == only_target]
    # face_filter (= matrix で同 face の 14-15 セルだけ trim、 face 単位の
    # 単発 trim 用)。 layered では face_<n> の 1 件にしかマッチしない。
    face_filter = params.get("face_filter")
    if (
        isinstance(face_filter, str) and face_filter
        and meta.mode == "matrix"
    ):
        inputs = [
            (name, src) for (name, src) in inputs
            if name.startswith(f"{face_filter}_")
        ]

    trimmed_dir = mgr.stage_dir(persona_id, set_name, ap.STAGE_TRIMMED)
    # 旧 trimmed を消すのは全件 trim の時だけ (= 単発 / face_filter では他を残す)。
    is_partial = bool(only_target) or bool(face_filter)
    if not is_partial and trimmed_dir.exists():
        for f in trimmed_dir.glob("*.png"):
            f.unlink()

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for (target_name, src_path) in inputs:
        # variant key (= matrix なら face、 layered なら target) で
        # override 引く。 なければ default。
        variant_key = _variant_key_for_target(target_name, meta.mode)
        rect = overrides.get(variant_key) or default_rect
        dst_path = trimmed_dir / f"{target_name}.png"
        try:
            _trim_one(src_path, dst_path, rect)
            results.append({
                "target": target_name,
                "path": str(dst_path),
                "used_override": variant_key in overrides,
                "variant_key": variant_key,
            })
        except Exception as exc:
            LOGGER.exception(
                "avatar_finalizer: trim failed %s -> %s: %s",
                src_path, dst_path, exc,
            )
            errors.append({"target": target_name, "error": str(exc)})

    # params 経由で渡された rect は metadata default にも反映。
    if params.get("trim_rect") is not None:
        mgr.update_metadata(persona_id, set_name, trim_rect=default_rect)

    LOGGER.info(
        "avatar_finalizer: stage 04_trimmed persona=%s set=%s "
        "ok=%d err=%d (mode=%s)",
        persona_id, set_name, len(results), len(errors), meta.mode,
    )
    return {
        "stage_id": ap.STAGE_TRIMMED,
        "files": results,
        "errors": errors,
    }


# ----- Stage 05: Resize + RGB565 + concat → avatar.bin -----


def _rgb888_to_rgb565_bytes(im) -> bytes:
    """PIL RGB image → little-endian RGB565 bytes (= convert_avatars.py と同じ
    pack 規約、 firmware の LV_COLOR_FORMAT_RGB565 と一致)。"""
    if im.mode != "RGB":
        im = im.convert("RGB")
    pixels = im.tobytes()
    out = bytearray(len(pixels) // 3 * 2)
    j = 0
    for i in range(0, len(pixels), 3):
        r = pixels[i]
        g = pixels[i + 1]
        b = pixels[i + 2]
        rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
        out[j] = rgb565 & 0xFF
        out[j + 1] = (rgb565 >> 8) & 0xFF
        j += 2
    return bytes(out)


def _convert_one_to_rgb565(src: Path) -> bytes:
    """1 枚 PNG → 160×120 にリサイズ + RGB565 raw bytes (38,400 bytes)。"""
    Image = _pil_image()
    with Image.open(src) as im:
        rgb = im.convert("RGB").resize(
            (TARGET_W, TARGET_H), Image.LANCZOS,
        )
    return _rgb888_to_rgb565_bytes(rgb)


def _layered_ordering() -> list[str]:
    """layered mode の出力 ordering (= firmware と一致)。"""
    return (
        [f"face_{f}" for f in ap.FACE_NAMES]
        + [f"eyes_{e}" for e in ap.EYES_STATES]
        + [f"mouth_{m}" for m in ap.MOUTH_SHAPES]
    )


def _matrix_ordering() -> list[str]:
    """matrix mode の出力 ordering (= firmware AvatarSet::GetMatrix の
    `face * 15 + eyes * 5 + mouth` lookup と一致)。"""
    out: list[str] = []
    for face in ap.FACE_NAMES:
        for eyes in ap.EYES_STATES:
            for mouth in ap.MOUTH_SHAPES:
                out.append(f"{face}_{eyes}_{mouth}")
    return out


def finalize_avatar_set(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
) -> dict[str, Any]:
    """⑤ リサイズ + RGB565 変換 → avatar.bin 連結 + manifest.json 生成。

    出力: <set_dir>/avatar.bin, <set_dir>/manifest.json
    """
    meta = mgr.read_metadata(persona_id, set_name)
    if meta is None:
        raise FileNotFoundError(f"WIP not found: {persona_id}/{set_name}")

    trimmed_dir = mgr.stage_dir(persona_id, set_name, ap.STAGE_TRIMMED)
    if not trimmed_dir.exists():
        raise FileNotFoundError(
            f"04_trimmed directory not found (run ④ first): {trimmed_dir}"
        )

    if meta.mode == "matrix":
        ordering = _matrix_ordering()
        expected_bytes = MATRIX_TOTAL_BYTES
    elif meta.mode == "layered":
        ordering = _layered_ordering()
        expected_bytes = LAYERED_TOTAL_BYTES
    else:
        raise ValueError(f"Unknown mode: {meta.mode!r}")

    chunks: list[bytes] = []
    converted_count = 0
    for name in ordering:
        src = trimmed_dir / f"{name}.png"
        if not src.exists():
            raise FileNotFoundError(
                f"Trimmed file missing for finalize: {src}"
            )
        chunk = _convert_one_to_rgb565(src)
        if len(chunk) != FRAME_BYTES:
            raise RuntimeError(
                f"Frame size mismatch for {src}: got {len(chunk)} "
                f"expected {FRAME_BYTES}"
            )
        chunks.append(chunk)
        converted_count += 1

    payload = b"".join(chunks)
    if len(payload) != expected_bytes:
        raise RuntimeError(
            f"Payload size mismatch: got {len(payload)} expected "
            f"{expected_bytes}"
        )

    checksum = "sha256:" + hashlib.sha256(payload).hexdigest()

    bin_path = mgr.finalized_bin_path(persona_id, set_name)
    manifest_path = mgr.finalized_manifest_path(persona_id, set_name)
    bin_path.parent.mkdir(parents=True, exist_ok=True)
    bin_path.write_bytes(payload)

    manifest: dict[str, Any] = {
        "mode": meta.mode,
        "checksum": checksum,
    }
    # 旧 manifest があれば tags (D-8 拡張余地) を保持。
    if manifest_path.exists():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(old, dict) and "tags" in old:
                manifest["tags"] = old["tags"]
        except Exception:
            pass
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    LOGGER.info(
        "avatar_finalizer: stage 05 finalize persona=%s set=%s "
        "frames=%d bytes=%d sha256=%s",
        persona_id, set_name, converted_count, len(payload), checksum,
    )

    # 該当ペルソナが現在 Vessel Building 内にいるなら、 確定品を即時転送
    # (= まはー検証 2026-05-17、 「次の入退室待ち」 を回避)。 失敗時は
    # warning に留めて finalize 自体は成功扱い (= 確定品は保存できてる、
    # 次回入室で自動転送される)。
    auto_transfer: Optional[dict[str, Any]] = None
    try:
        from avatar_loader import get_avatar_loader
        if get_avatar_loader().is_persona_in_vessel(persona_id):
            LOGGER.info(
                "avatar_finalizer: persona=%s is in Vessel → "
                "auto-transferring after finalize",
                persona_id,
            )
            try:
                auto_transfer = transfer_avatar_set(
                    mgr, persona_id, set_name,
                )
            except Exception as transfer_exc:
                LOGGER.warning(
                    "avatar_finalizer: auto-transfer failed "
                    "(finalize itself succeeded): %s",
                    transfer_exc,
                )
                auto_transfer = {"error": str(transfer_exc)}
    except Exception as outer:
        LOGGER.warning(
            "avatar_finalizer: in-vessel check failed: %s", outer,
        )

    return {
        "stage_id": "05_finalize",
        "bin_path": str(bin_path),
        "manifest_path": str(manifest_path),
        "checksum": checksum,
        "bytes": len(payload),
        "frames": converted_count,
        "mode": meta.mode,
        "auto_transferred": (
            auto_transfer is not None
            and "error" not in auto_transfer
        ),
        "auto_transfer_detail": auto_transfer,
    }


# ----- Stage 06: Transfer to Stack-chan -----


def transfer_avatar_set(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
) -> dict[str, Any]:
    """⑥ 確定品 (= avatar.bin + manifest.json) を Stack-chan device に転送。

    既存 avatar_loader._call_load_avatar_set + _run_async_on_mcp_loop を流用。
    Vessel ペアリング済み + gateway 接続中である必要がある。
    """
    bin_path = mgr.finalized_bin_path(persona_id, set_name)
    manifest_path = mgr.finalized_manifest_path(persona_id, set_name)
    if not bin_path.exists():
        raise FileNotFoundError(
            f"avatar.bin not found (run ⑤ first): {bin_path}"
        )
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"manifest.json not found: {manifest_path}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Failed to parse manifest: {manifest_path}: {exc}"
        )
    mode = manifest.get("mode")
    if mode not in ap.VALID_MODES:
        raise ValueError(f"Invalid mode in manifest: {mode!r}")

    # avatar_loader の MCP 呼び出しヘルパを流用。
    from avatar_loader import (  # noqa: E402
        _LOAD_TIMEOUT_SEC,
        _call_load_avatar_set,
        _run_async_on_mcp_loop,
        get_avatar_loader,
    )

    result = _run_async_on_mcp_loop(
        _call_load_avatar_set(str(bin_path), mode),
        timeout_sec=_LOAD_TIMEOUT_SEC,
    )
    if result is None:
        raise RuntimeError(
            "load_avatar_set call failed or timed out "
            "(check ~/.saiverse/user_data/logs/<session>/backend.log)"
        )

    # 転送成功時は avatar_loader の in-memory cache にも反映 (= 続けて
    # Vessel 入室した時に同 checksum スキップが効くように)。
    checksum = manifest.get("checksum")
    if checksum:
        get_avatar_loader().mark_loaded(persona_id, checksum)

    LOGGER.info(
        "avatar_finalizer: stage 06 transfer persona=%s set=%s "
        "mode=%s result=%s",
        persona_id, set_name, mode, result,
    )
    return {
        "stage_id": "06_transfer",
        "mode": mode,
        "checksum": checksum,
        "result": str(result),
    }


# ----- Stage 01 alt: 手動アップロード経路 (Phase 4.5-d 追補) -----
#
# まはー指摘: ① で外部画像をそのまま base に使う経路が必要 (= 既にペルソナ
# の標準顔画像がある場合)。 そして ①②③ で目パチ口パクの座標ズレを起こ
# さないため、 アップロード画像はその時点で「生成に適したアス比」 にクロップ
# する。 metadata.aspect_ratio をその値に揃えて、 ②③ も同アス比で生成
# されるようにする。

# image_generator が対応する代表アス比 (= 全 backend 共通で安定するもの
# のうち、 目パチ口パク用途で意味のある正方形 / 横長 / 縦長を網羅)。
SUPPORTED_ASPECTS: dict[str, tuple[float, float]] = {
    "1:1": (1.0, 1.0),
    "4:3": (4.0, 3.0),
    "3:4": (3.0, 4.0),
    "3:2": (3.0, 2.0),
    "2:3": (2.0, 3.0),
    "16:9": (16.0, 9.0),
    "9:16": (9.0, 16.0),
    "4:5": (4.0, 5.0),
    "5:4": (5.0, 4.0),
}


def closest_supported_aspect(width: int, height: int) -> str:
    """画像サイズから最も近い対応アスペクト比を選ぶ。"""
    if width <= 0 or height <= 0:
        return "1:1"
    actual = width / height
    best = "1:1"
    best_diff = float("inf")
    for name, (rw, rh) in SUPPORTED_ASPECTS.items():
        diff = abs(actual - (rw / rh))
        if diff < best_diff:
            best_diff = diff
            best = name
    return best


def _center_crop_to_aspect(
    width: int, height: int, target_aspect: str,
) -> tuple[int, int, int, int]:
    """中央クロップで target_aspect に揃える矩形 (x, y, w, h) を返す。"""
    target_rw, target_rh = SUPPORTED_ASPECTS[target_aspect]
    target_ratio = target_rw / target_rh
    current_ratio = width / height
    if abs(current_ratio - target_ratio) < 1e-6:
        return 0, 0, width, height
    if current_ratio > target_ratio:
        # 横長すぎる → 左右を切る
        cw = int(height * target_ratio)
        ch = height
        x = (width - cw) // 2
        y = 0
    else:
        # 縦長すぎる → 上下を切る
        cw = width
        ch = int(width / target_ratio)
        x = 0
        y = (height - ch) // 2
    return x, y, cw, ch


def upload_face_image(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
    image_bytes: bytes,
    target_aspect: str,
    crop_rect: Optional[dict[str, int]] = None,
) -> dict[str, Any]:
    """① 手動アップロード経路。

    アップロード画像をクロップして `wip/01_face/face.png` に保存。
    `metadata.aspect_ratio` は「実際に切り出した矩形に最も近い対応アス比」
    に更新する (= ②③ も同アス比で生成、 目パチ口パクの座標ズレ防止)。
    target_aspect は crop_rect 未指定時の中央クロップ比率としてのみ使う。

    Args:
        image_bytes: アップロード画像 (= 何形式でも PIL が読めれば OK)
        target_aspect: SUPPORTED_ASPECTS のキー (= crop_rect 無し時の中央
                       クロップ比率。 crop_rect 有り時は実寸から導出した
                       値が優先される)
        crop_rect: optional {"x", "y", "width", "height"}。 None なら中央クロップ
    """
    if target_aspect not in SUPPORTED_ASPECTS:
        raise ValueError(
            f"Unsupported aspect_ratio: {target_aspect!r} "
            f"(allowed: {sorted(SUPPORTED_ASPECTS)})"
        )
    meta = mgr.read_metadata(persona_id, set_name)
    if meta is None:
        raise FileNotFoundError(f"WIP not found: {persona_id}/{set_name}")

    Image = _pil_image()
    import io
    with Image.open(io.BytesIO(image_bytes)) as im:
        rgb = im.convert("RGB")
        w, h = rgb.size
        if crop_rect:
            x = int(crop_rect["x"])
            y = int(crop_rect["y"])
            cw = int(crop_rect["width"])
            ch = int(crop_rect["height"])
            # 範囲チェック
            if x < 0 or y < 0 or cw <= 0 or ch <= 0:
                raise ValueError(
                    f"Invalid crop_rect: {crop_rect} (negative or zero size)"
                )
            if x + cw > w or y + ch > h:
                raise ValueError(
                    f"crop_rect out of bounds: image={w}x{h}, "
                    f"rect={crop_rect}"
                )
        else:
            x, y, cw, ch = _center_crop_to_aspect(w, h, target_aspect)
        cropped = rgb.crop((x, y, x + cw, y + ch))

    out_path = (
        mgr.stage_dir(persona_id, set_name, ap.STAGE_FACE) / "face.png"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cropped.save(out_path, format="PNG")

    # metadata の aspect_ratio は「実際に切り出した矩形」 から導出する
    # (= ②③ も同アス比で生成)。 UI のセレクト値 (target_aspect) と crop
    # 矩形の実アス比が食い違っても、 face.png の見た目 = 真実。
    # 2026-06-10: ① で 4:3 にトリミングしたのに target_aspect=1:1 が
    # 送られて ②③ が 1:1 生成された事故の再発防止。
    effective_aspect = closest_supported_aspect(cw, ch)
    if effective_aspect != target_aspect:
        LOGGER.warning(
            "avatar_finalizer: ① upload aspect mismatch: "
            "target_aspect=%s but crop %dx%d is closest to %s "
            "-> metadata adopts %s",
            target_aspect, cw, ch, effective_aspect, effective_aspect,
        )
    mgr.update_metadata(
        persona_id, set_name, aspect_ratio=effective_aspect,
    )

    LOGGER.info(
        "avatar_finalizer: ① upload persona=%s set=%s "
        "original=%dx%d crop=(%d,%d,%d,%d) target_aspect=%s "
        "effective_aspect=%s",
        persona_id, set_name, w, h, x, y, cw, ch, target_aspect,
        effective_aspect,
    )
    return {
        "stage_id": ap.STAGE_FACE,
        "path": str(out_path),
        "original_size": [w, h],
        "crop": {"x": x, "y": y, "width": cw, "height": ch},
        "target_aspect": effective_aspect,
        "requested_aspect": target_aspect,
        "cropped_size": [cw, ch],
    }


def analyze_image(image_bytes: bytes) -> dict[str, Any]:
    """アップロード前のプレビュー用: 画像サイズ + 推奨アス比を返す。"""
    Image = _pil_image()
    import io
    with Image.open(io.BytesIO(image_bytes)) as im:
        w, h = im.size
    suggested = closest_supported_aspect(w, h)
    return {
        "width": w,
        "height": h,
        "suggested_aspect": suggested,
        "supported_aspects": sorted(SUPPORTED_ASPECTS),
    }


def upload_reference_image(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
    image_bytes: bytes,
    filename_hint: str = "ref.png",
) -> dict[str, Any]:
    """① 生成経路で使う参照画像を WIP 内に保存する。

    保存先: `wip/01_face/refs/<sanitized_name>` (= face.png と衝突回避)。
    generate_stage_face は params["ref_image_paths"] でこの path を受け取る。

    Returns:
        {"path": <絶対パス>, "name": <保存名>}
    """
    meta = mgr.read_metadata(persona_id, set_name)
    if meta is None:
        raise FileNotFoundError(f"WIP not found: {persona_id}/{set_name}")

    refs_dir = (
        mgr.stage_dir(persona_id, set_name, ap.STAGE_FACE) / "refs"
    )
    refs_dir.mkdir(parents=True, exist_ok=True)

    # filename を sanitize (= ../ や path 区切り除去、 拡張子は元のを尊重)。
    safe_name = Path(filename_hint).name
    if not safe_name or safe_name.startswith("."):
        safe_name = "ref.png"
    out_path = refs_dir / safe_name

    # 既存と衝突したら連番付け。
    idx = 1
    while out_path.exists():
        stem = Path(safe_name).stem
        suffix = Path(safe_name).suffix or ".png"
        out_path = refs_dir / f"{stem}_{idx}{suffix}"
        idx += 1

    out_path.write_bytes(image_bytes)
    LOGGER.info(
        "avatar_finalizer: uploaded ref image persona=%s set=%s -> %s",
        persona_id, set_name, out_path,
    )
    return {
        "path": str(out_path),
        "name": out_path.name,
    }


def list_reference_images(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
) -> list[dict[str, Any]]:
    """① 生成経路で使う保存済み参照画像の一覧を返す。"""
    refs_dir = (
        mgr.stage_dir(persona_id, set_name, ap.STAGE_FACE) / "refs"
    )
    if not refs_dir.exists():
        return []
    out: list[dict[str, Any]] = []
    for entry in sorted(refs_dir.iterdir()):
        if entry.is_file() and entry.suffix.lower() in (
            ".png", ".jpg", ".jpeg", ".webp",
        ):
            out.append({"path": str(entry), "name": entry.name})
    return out


def delete_reference_image(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
    name: str,
) -> bool:
    """参照画像 1 個を削除。"""
    safe_name = Path(name).name
    if not safe_name or safe_name.startswith("."):
        return False
    refs_dir = (
        mgr.stage_dir(persona_id, set_name, ap.STAGE_FACE) / "refs"
    )
    target = refs_dir / safe_name
    if not target.exists():
        return False
    target.unlink()
    return True


# ----- Stage 04 alt: zip / フォルダ直接投入 (Phase 4.5-d-5) -----


def expected_trimmed_filenames(mode: str) -> set[str]:
    """mode に応じて 04_trimmed に置くべきファイル名 (= .png) のセット。

    matrix : 90 個の `{face}_{eyes}_{mouth}.png`
    layered: 14 個 (= face_<name> × 6 + eyes_<state> × 3 + mouth_<shape> × 5)
    """
    if mode == "matrix":
        return {
            f"{face}_{eyes}_{mouth}.png"
            for face in ap.FACE_NAMES
            for eyes in ap.EYES_STATES
            for mouth in ap.MOUTH_SHAPES
        }
    if mode == "layered":
        out: set[str] = set()
        for face in ap.FACE_NAMES:
            out.add(f"face_{face}.png")
        for state in ap.EYES_STATES:
            out.add(f"eyes_{state}.png")
        for shape in ap.MOUTH_SHAPES:
            out.add(f"mouth_{shape}.png")
        return out
    raise ValueError(f"Unknown mode: {mode!r}")


def import_trimmed_zip(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
    zip_bytes: bytes,
    *,
    require_complete: bool = True,
) -> dict[str, Any]:
    """zip から ④ 04_trimmed/ に直接展開する経路 (D-7)。

    既存 04_trimmed/ は消してから書く (= 部分残しによる混乱を避ける)。
    zip 内のディレクトリ構造は無視 (= basename だけ取り出す = zip slip 防止)。

    Args:
        zip_bytes: zip ファイル本体
        require_complete: True なら expected ファイルが全て揃っていることを
            要求 (= 不足があれば ValueError)。 False なら部分投入を許容

    Returns:
        {"extracted": N, "expected": M, "missing": [...], "skipped": [...]}
    """
    import io
    import zipfile

    meta = mgr.read_metadata(persona_id, set_name)
    if meta is None:
        raise FileNotFoundError(f"WIP not found: {persona_id}/{set_name}")
    expected = expected_trimmed_filenames(meta.mode)

    trimmed_dir = mgr.stage_dir(persona_id, set_name, ap.STAGE_TRIMMED)
    if trimmed_dir.exists():
        for f in trimmed_dir.glob("*.png"):
            f.unlink()
    trimmed_dir.mkdir(parents=True, exist_ok=True)

    extracted: list[str] = []
    skipped: list[dict[str, str]] = []

    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for member in zf.namelist():
                if member.endswith("/"):
                    continue  # ディレクトリエントリは無視
                basename = Path(member).name
                # zip slip 対策: basename を取った時点で `..` / `/` は除去される。
                if not basename or basename.startswith("."):
                    skipped.append({
                        "name": member, "reason": "hidden or invalid",
                    })
                    continue
                if basename not in expected:
                    skipped.append({
                        "name": basename, "reason": "not in expected set",
                    })
                    continue
                dst = trimmed_dir / basename
                with zf.open(member) as src:
                    dst.write_bytes(src.read())
                extracted.append(basename)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Invalid zip file: {exc}")

    missing = sorted(expected - set(extracted))

    if require_complete and missing:
        # ロールバック (= 部分投入で `04_trimmed/` が中途半端な状態にならないように)。
        for name in extracted:
            (trimmed_dir / name).unlink(missing_ok=True)
        raise ValueError(
            f"Missing expected files for {meta.mode} mode: "
            f"{len(missing)} missing (first 5: {missing[:5]})"
        )

    # ④ 段階完了マーク (= ⑤⑥ にすぐ進める状態にする)。
    if not missing:
        mgr.mark_stage_completed(persona_id, set_name, ap.STAGE_TRIMMED)
    # cache buster invalidate (= 04_trimmed の画像が差し替わったので
    # frontend の `?t=${updated_at}` で古画像が browser cache されないように)。
    mgr.touch_updated_at(persona_id, set_name)

    LOGGER.info(
        "avatar_finalizer: import_zip persona=%s set=%s "
        "extracted=%d expected=%d missing=%d skipped=%d (mode=%s)",
        persona_id, set_name, len(extracted), len(expected),
        len(missing), len(skipped), meta.mode,
    )
    return {
        "stage_id": ap.STAGE_TRIMMED,
        "mode": meta.mode,
        "extracted": len(extracted),
        "expected": len(expected),
        "missing": missing,
        "skipped": skipped,
    }


__all__ = [
    "TARGET_W",
    "TARGET_H",
    "FRAME_BYTES",
    "LAYERED_TOTAL_BYTES",
    "MATRIX_TOTAL_BYTES",
    "SUPPORTED_ASPECTS",
    "generate_stage_trim",
    "finalize_avatar_set",
    "transfer_avatar_set",
    "expected_trimmed_filenames",
    "import_trimmed_zip",
    "upload_face_image",
    "analyze_image",
    "closest_supported_aspect",
    "upload_reference_image",
    "list_reference_images",
    "delete_reference_image",
]
