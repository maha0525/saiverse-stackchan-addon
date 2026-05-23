"""Stack-chan Vessel: avatar セット画像生成エンジン (Phase 4.5-d-2)。

avatar_pipeline.py の stage executor / regenerate executor hook を実装する。
段階 ①②③ (= 元顔 / 表情差分 5 種 / 目・口差分) の画像を image_generator
backend 経由で生成し、 WIP の所定ディレクトリに PNG 保存する。

並列実行は ThreadPoolExecutor (= image_generator が sync 関数なので)、
並列度は AvatarSetMetadata.parallelism で制御。

口の半開き 2 段戦略:
  1. 1 段目: プロンプトテンプレートに「閉じと開きの中間補完として使える画像」
     を明示。 これがデフォルト動作で `DEFAULT_TEMPLATES["mouth"]["half"]` に
     文言が入っている
  2. fallback: 1 段目で品質が出ない時、 mouth=open を先に生成しておき、
     mouth=half 生成時に閉じ + 開き両方を input_images に渡す。 これは
     Phase 4.5-d-2 では実装せず、 UI から fallback モードを発火する余地
     だけ残しておく (= params["use_open_reference"]=True で発動)

詳細: docs/intent/stackchan_avatar_pipeline.md §D-4 (③) §D-5 §D-6
"""
import importlib.util
import logging
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

_PACK_DIR = str(Path(__file__).parent)
if _PACK_DIR not in sys.path:
    sys.path.insert(0, _PACK_DIR)

import avatar_pipeline as ap  # noqa: E402

LOGGER = logging.getLogger(__name__)

# image_generator.py のパス (= builtin_data/tools/image_generator.py)。
# expansion_data/<addon>/ の 2 階層上 = repo root。
_REPO_ROOT = Path(__file__).resolve().parents[2]
_IMAGE_GEN_PATH = _REPO_ROOT / "builtin_data" / "tools" / "image_generator.py"


# ----- Default prompt templates (= 4.5-d-4 UI の初期値用) -----

DEFAULT_TEMPLATES: dict[str, Any] = {
    "common_prompt_hint": (
        "ペルソナの外見を書く: 顔立ち、 髪色・髪型、 服装、 雰囲気など"
    ),
    # ③ で常に prepend される「ポーズ・構図維持」 制約 (= まはー検証 2026-05-17)。
    # 「指定がない側 (目または口) は参照画像から完全にそのまま」 を明示
    # (= eyes=open 等で目プロンプトを省略した時に AI が目を変えてしまう事故防止、
    # 例: happy_open_half で目が完全閉になる事象)。
    # metadata.extra_prompts["03_constraint"]["all"] で上書き可能。
    "03_constraint": {
        "all": (
            "参照画像をもとに、 瞬き・口パクアニメーションをさせるための"
            "差分画像制作である。 構図・ポーズ・表す感情・服装などは"
            "一切変えないこと。 さらに、 目と口のうち以下に指定がある側"
            "のみを指定に合わせて変更し、 指定がない側 (目または口) は"
            "参照画像から完全にそのままにすること。"
        ),
    },
    # ③ で目を編集する (= eyes != "open") セルに追加される hint
    # (= happy / sad で half が open より開く事故防止、 まはー検証 2026-05-17)。
    # 参照画像が「この表情における目の最大開き状態」 だと明示することで、
    # half / closed 生成時の開き上限が固定される。
    "03_eye_modify_hint": {
        "all": (
            "参照画像はこの表情における目の最大開き状態である。 "
            "そこから目の開きを減少させて指定の状態にすること。"
        ),
    },
    ap.STAGE_FACE: {
        "face": (
            "Neutral expression, looking straight at the camera, "
            "calm and relaxed, idle face"
        ),
    },
    ap.STAGE_EXPRESSIONS: {
        "happy": (
            "Smiling brightly, eyes slightly narrowed, "
            "corners of mouth raised, cheerful expression"
        ),
        "thinking": (
            "Thoughtful expression, slight tilt of head, "
            "eyes focused on a distant point, lips slightly pursed"
        ),
        "sad": (
            "Sad expression, downcast eyes, slight frown, lips drawn together"
        ),
        "surprised": (
            "Surprised expression, eyes wide open, eyebrows raised, "
            "mouth slightly open in astonishment"
        ),
        "embarrassed": (
            "Embarrassed expression, slight blush on cheeks, "
            "eyes averted, lips slightly pursed"
        ),
    },
    "eyes": {
        "open": "eyes fully open",
        "half": "eyes half closed, halfway between open and closed",
        "closed": "eyes fully closed",
    },
    "mouth": {
        "closed": "mouth closed, lips fully together",
        "half": (
            "lips slightly parted, just a small gap visible between the lips. "
            "This is a halfway state between mouth fully closed and mouth open, "
            "useful as an intermediate frame for mouth animation interpolation "
            "(NOT a speaking pose, but a quiet intermediate frame)"
        ),
        "open": "mouth open, lips clearly apart, speaking pose",
        "e": (
            "mouth in 'e' shape, lips stretched horizontally to pronounce "
            "the 'e' sound, slight smile"
        ),
        "u": (
            "mouth in 'u' shape, lips rounded to pronounce the 'u' sound, "
            "lips pushed slightly forward"
        ),
    },
}


# ----- image_generator dynamic load -----

_image_generator_module = None


def _get_image_generator():
    """builtin_data/tools/image_generator.py を spec_from_file_location で
    ロード。 グローバルキャッシュ。"""
    global _image_generator_module
    if _image_generator_module is not None:
        return _image_generator_module
    if not _IMAGE_GEN_PATH.exists():
        raise FileNotFoundError(
            f"image_generator.py not found at {_IMAGE_GEN_PATH}"
        )
    spec = importlib.util.spec_from_file_location(
        "_avatar_addon_image_generator", _IMAGE_GEN_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to spec image_generator: {_IMAGE_GEN_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_avatar_addon_image_generator"] = module
    spec.loader.exec_module(module)
    _image_generator_module = module
    return module


# backend 名 → image_generator 内の private 関数名 mapping。
_BACKEND_DISPATCH = {
    "nano_banana_2": "_generate_with_nano_banana_2",
    "nano_banana_pro": "_generate_with_nano_banana_pro",
    "gpt_image_1_5": "_generate_with_gpt_image_1_5",
    "gpt_image_2": "_generate_with_gpt_image_2",
    "grok_imagine": "_generate_with_grok_imagine",
}


def _generate_one(
    prompt: str,
    model: str,
    input_image_paths: Optional[list[Path]] = None,
    aspect_ratio: str = "1:1",
    quality: str = "high",
) -> tuple[bytes, str]:
    """1 枚生成。 image_generator の backend 関数を直接呼ぶ。

    Returns:
        (image_bytes, mime_type)

    ログ方針 (memory 「ロギングは実装時点で」):
      - 呼び出し前に prompt 全文 / model / aspect / quality / refs を INFO
      - 例外時は OpenAI 等 SDK の response body を含めて ERROR (= 「再現
        してログ追加」 を回避するため)
    """
    if model not in _BACKEND_DISPATCH:
        raise ValueError(
            f"Unknown image model: {model!r} "
            f"(allowed: {list(_BACKEND_DISPATCH)})"
        )
    ig = _get_image_generator()
    func_name = _BACKEND_DISPATCH[model]
    func = getattr(ig, func_name, None)
    if func is None:
        raise RuntimeError(
            f"image_generator backend function not found: {func_name}"
        )

    ref_paths_str = (
        [str(p) for p in input_image_paths] if input_image_paths else []
    )
    LOGGER.info(
        "avatar_generator: _generate_one BEGIN model=%s aspect=%s quality=%s "
        "refs=%s prompt_len=%d",
        model, aspect_ratio, quality, ref_paths_str, len(prompt or ""),
    )
    LOGGER.info(
        "avatar_generator: _generate_one PROMPT >>>\n%s\n<<<",
        prompt,
    )

    try:
        result = func(
            prompt=prompt,
            aspect_ratio=aspect_ratio,
            quality=quality,
            input_image_paths=input_image_paths,
        )
    except Exception as exc:
        # OpenAI / Anthropic / Gemini SDK の例外は response body / message を
        # 含むことが多い。 そこを抽出して詳細 ERROR ログ (= 「再現してログ追加」
        # を防ぐ、 memory 原則)。
        detail_parts: list[str] = [
            f"type={type(exc).__name__}", f"msg={exc!s}",
        ]
        # OpenAI / httpx 由来の response 属性 (= status_code / body / text)。
        for attr in ("status_code", "code", "type"):
            v = getattr(exc, attr, None)
            if v is not None:
                detail_parts.append(f"{attr}={v}")
        # OpenAI v1 SDK の body (= dict)
        body = getattr(exc, "body", None)
        if body is not None:
            detail_parts.append(f"body={body!r}")
        # OpenAI v1 SDK の response 属性 (= httpx.Response)
        response = getattr(exc, "response", None)
        if response is not None:
            try:
                detail_parts.append(f"response_text={response.text!r}")
            except Exception:
                detail_parts.append(
                    f"response_type={type(response).__name__}",
                )
        LOGGER.exception(
            "avatar_generator: _generate_one FAILED model=%s aspect=%s "
            "quality=%s refs=%s | %s | prompt:\n%s",
            model, aspect_ratio, quality, ref_paths_str,
            " ".join(detail_parts), prompt,
        )
        raise

    img_bytes, mime = result
    LOGGER.info(
        "avatar_generator: _generate_one OK model=%s bytes=%d mime=%s",
        model, len(img_bytes or b""), mime,
    )
    return result


# ----- Prompt building -----


def _build_prompt(*parts: str) -> str:
    """各部分を ". " で連結 (= 空文字 / None はスキップ)。"""
    pieces: list[str] = []
    for p in parts:
        if not p:
            continue
        text = p.strip()
        if text:
            pieces.append(text)
    return ". ".join(pieces)


def _get_extra_prompt(
    meta: ap.AvatarSetMetadata, stage_id: str, target: str,
) -> str:
    """meta.extra_prompts[stage_id][target] を取り出す (無ければ default テンプレ)。"""
    extras = meta.extra_prompts.get(stage_id, {})
    if isinstance(extras, dict):
        val = extras.get(target)
        if isinstance(val, str) and val.strip():
            return val
    defaults = DEFAULT_TEMPLATES.get(stage_id, {})
    if isinstance(defaults, dict):
        return defaults.get(target, "")
    return ""


def _get_eyes_prompt(meta: ap.AvatarSetMetadata, eyes_state: str) -> str:
    """目状態の追加自由文 (= meta に独自設定があればそれ、 無ければ default)。"""
    custom = meta.extra_prompts.get("eyes", {})
    if isinstance(custom, dict):
        val = custom.get(eyes_state)
        if isinstance(val, str) and val.strip():
            return val
    return DEFAULT_TEMPLATES["eyes"].get(eyes_state, "")


def _get_mouth_prompt(meta: ap.AvatarSetMetadata, mouth_shape: str) -> str:
    custom = meta.extra_prompts.get("mouth", {})
    if isinstance(custom, dict):
        val = custom.get(mouth_shape)
        if isinstance(val, str) and val.strip():
            return val
    return DEFAULT_TEMPLATES["mouth"].get(mouth_shape, "")


def _resolve_quality(meta: ap.AvatarSetMetadata, stage_id: str) -> str:
    """段階別 override があればそれ、 なければセット全体の image_quality。"""
    overrides = meta.stage_quality_overrides or {}
    return overrides.get(stage_id, meta.image_quality)


def _get_stage3_constraint_prompt(meta: ap.AvatarSetMetadata) -> str:
    """③ で必ず prepend する「ポーズ・構図維持」 制約。

    metadata.extra_prompts["03_constraint"]["all"] で上書き可能、
    なければ DEFAULT_TEMPLATES から取る。 空にしたい時は空文字を入れる。
    """
    extras = meta.extra_prompts.get("03_constraint", {})
    if isinstance(extras, dict):
        val = extras.get("all")
        if isinstance(val, str):
            # ユーザーが明示的に空文字に設定したら省略 (= 上書きで無効化可能)
            return val.strip()
    return DEFAULT_TEMPLATES["03_constraint"]["all"]


def _common_for_stage3(meta: ap.AvatarSetMetadata) -> str:
    """③ で common_prompt を使うかの解決。 default OFF。"""
    if meta.apply_common_prompt_to_stage3:
        return meta.common_prompt or ""
    return ""


def _get_eye_modify_hint(meta: ap.AvatarSetMetadata) -> str:
    """目を編集するセルに追加する「最大開き hint」 (eyes != open 時のみ使う)。"""
    extras = meta.extra_prompts.get("03_eye_modify_hint", {})
    if isinstance(extras, dict):
        val = extras.get("all")
        if isinstance(val, str):
            return val.strip()
    return DEFAULT_TEMPLATES["03_eye_modify_hint"]["all"]


def _stage3_face_label(face: str) -> str:
    """③ で face を端的に明示する文。

    まはー検証 (2026-05-17): ②の長文表情テンプレ (= 「sad, downcast eyes,
    lips drawn together」 等の口形状まで踏み込んだ詳細) を ③ に持ち込むと、
    「mouth open」 指示と矛盾して AI が 2 枚並べた画像を返す事故が発生する。
    ③ では face 種別の端的な明示だけにする。
    """
    if face == "idle":
        return "neutral expression"
    return f"{face} expression"


def _resolve_aspect_ratio(
    meta: ap.AvatarSetMetadata, stage_id: str,
) -> str:
    """段階別 override があればそれ、 なければセット全体の aspect_ratio。

    通常 ② ③ は ① と同じアス比で生成すべき (= 目パチ口パクの座標ズレ防止、
    まはー指摘) なので、 stage_aspect_overrides は Debug 時の検証用。
    """
    overrides = meta.stage_aspect_overrides or {}
    return overrides.get(stage_id, meta.aspect_ratio)


# ----- Path helpers -----


def _stage_face_file(
    mgr: ap.AvatarPipelineManager, persona_id: str, set_name: str,
) -> Path:
    return mgr.stage_dir(persona_id, set_name, ap.STAGE_FACE) / "face.png"


def _stage_expression_file(
    mgr: ap.AvatarPipelineManager, persona_id: str, set_name: str, expr: str,
) -> Path:
    return mgr.stage_dir(
        persona_id, set_name, ap.STAGE_EXPRESSIONS,
    ) / f"{expr}.png"


def _stage_matrix_file(
    mgr: ap.AvatarPipelineManager, persona_id: str, set_name: str,
    face: str, eyes: str, mouth: str,
) -> Path:
    return mgr.stage_dir(
        persona_id, set_name, ap.STAGE_MATRIX,
    ) / f"{face}_{eyes}_{mouth}.png"


def _stage_layered_file(
    mgr: ap.AvatarPipelineManager, persona_id: str, set_name: str,
    kind: str, name: str,
) -> Path:
    # kind: "eyes" or "mouth"
    return mgr.stage_dir(
        persona_id, set_name, ap.STAGE_LAYERED,
    ) / f"{kind}_{name}.png"


def _write_png(out_path: Path, image_bytes: bytes, mime: str) -> None:
    """生成画像を WIP 内に書き込む。 PNG 以外は警告 (= 拡張子は .png 固定)。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if mime != "image/png":
        LOGGER.warning(
            "avatar_generator: backend returned mime=%s (not image/png), "
            "writing to %s as-is",
            mime, out_path,
        )
    out_path.write_bytes(image_bytes)


def _persist_extra_prompt_override(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
    stage_key: str,
    target: str,
    extra_prompt: str,
) -> None:
    """単発再生成で extra_prompt が上書きされた場合、 metadata にも反映する
    (= 次回の execute_stage / regenerate でも同じプロンプトが使える)。"""
    meta = mgr.read_metadata(persona_id, set_name)
    if meta is None:
        return
    extras = dict(meta.extra_prompts)
    stage_extras = dict(extras.get(stage_key, {}))
    stage_extras[target] = extra_prompt
    extras[stage_key] = stage_extras
    mgr.update_metadata(persona_id, set_name, extra_prompts=extras)


# ----- Stage generators -----


def generate_stage_face(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """① 元顔生成。

    params:
      extra_prompt: optional, 段階別追加自由文の上書き (= metadata の
                    extra_prompts[01_face][face] を一時的に置き換える)
      ref_image_paths: optional list[str], 参照画像 (= 任意アップロード)
    """
    meta = mgr.read_metadata(persona_id, set_name)
    if meta is None:
        raise FileNotFoundError(
            f"WIP metadata not found: persona={persona_id} name={set_name}"
        )
    extra = _get_extra_prompt(meta, ap.STAGE_FACE, "face")
    override = params.get("extra_prompt")
    if isinstance(override, str) and override.strip():
        extra = override
    prompt = _build_prompt(meta.common_prompt, extra)
    if not prompt:
        raise ValueError(
            "Empty prompt: both common_prompt and extra_prompt are missing"
        )

    ref_paths_raw = params.get("ref_image_paths") or []
    ref_paths = [Path(p) for p in ref_paths_raw if Path(p).exists()]

    image_bytes, mime = _generate_one(
        prompt=prompt,
        model=meta.image_model,
        input_image_paths=ref_paths or None,
        aspect_ratio=_resolve_aspect_ratio(meta, ap.STAGE_FACE),
        quality=_resolve_quality(meta, ap.STAGE_FACE),
    )
    out_path = _stage_face_file(mgr, persona_id, set_name)
    _write_png(out_path, image_bytes, mime)

    if isinstance(override, str) and override.strip():
        _persist_extra_prompt_override(
            mgr, persona_id, set_name, ap.STAGE_FACE, "face", override,
        )

    LOGGER.info(
        "avatar_generator: stage 01_face generated persona=%s set=%s -> %s",
        persona_id, set_name, out_path,
    )
    return {
        "stage_id": ap.STAGE_FACE,
        "files": [{"target": "face", "path": str(out_path)}],
        "errors": [],
    }


def generate_stage_expressions(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """② 表情差分 5 種生成 (= ① の元顔を base に happy / thinking / sad /
    surprised / embarrassed を並列生成)。"""
    meta = mgr.read_metadata(persona_id, set_name)
    if meta is None:
        raise FileNotFoundError(
            f"WIP metadata not found: persona={persona_id} name={set_name}"
        )
    face_path = _stage_face_file(mgr, persona_id, set_name)
    if not face_path.exists():
        raise FileNotFoundError(
            f"Stage 01_face output not found (run ① first): {face_path}"
        )

    parallelism = max(1, int(meta.parallelism or 5))
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    aspect = _resolve_aspect_ratio(meta, ap.STAGE_EXPRESSIONS)
    quality = _resolve_quality(meta, ap.STAGE_EXPRESSIONS)
    only_target = params.get("only_target")
    skip_existing = bool(params.get("skip_existing", False))
    targets = (
        [only_target] if only_target in ap.EXPRESSION_NAMES
        else list(ap.EXPRESSION_NAMES)
    )
    if skip_existing:
        targets = [
            t for t in targets
            if not _stage_expression_file(
                mgr, persona_id, set_name, t,
            ).exists()
        ]
    with ThreadPoolExecutor(max_workers=parallelism) as ex:
        futures = {}
        for expr in targets:
            extra = _get_extra_prompt(meta, ap.STAGE_EXPRESSIONS, expr)
            prompt = _build_prompt(meta.common_prompt, extra)
            futures[ex.submit(
                _generate_one,
                prompt=prompt,
                model=meta.image_model,
                input_image_paths=[face_path],
                aspect_ratio=aspect,
                quality=quality,
            )] = expr
        for fut in as_completed(futures):
            expr = futures[fut]
            try:
                image_bytes, mime = fut.result()
                out_path = _stage_expression_file(
                    mgr, persona_id, set_name, expr,
                )
                _write_png(out_path, image_bytes, mime)
                results.append({
                    "target": expr, "path": str(out_path),
                })
            except Exception as exc:
                LOGGER.exception(
                    "avatar_generator: stage 02_expressions failed expr=%s "
                    "persona=%s set=%s: %s",
                    expr, persona_id, set_name, exc,
                )
                errors.append({"target": expr, "error": str(exc)})

    LOGGER.info(
        "avatar_generator: stage 02_expressions persona=%s set=%s "
        "ok=%d err=%d",
        persona_id, set_name, len(results), len(errors),
    )
    return {
        "stage_id": ap.STAGE_EXPRESSIONS,
        "files": results,
        "errors": errors,
    }


def generate_stage_matrix(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """③ 目・口差分生成 (matrix mode、 84 枚 + base 6 枚コピー = 計 90 枚)。

    各表情 (face) × 目 3 × 口 5 = 15 通り、 そのうち base (eyes=open,
    mouth=closed) は ② / ① の画像と同一なのでコピーで対応、 残り 14 通り
    × 6 表情 = 84 枚を新規生成する。
    """
    meta = mgr.read_metadata(persona_id, set_name)
    if meta is None:
        raise FileNotFoundError(
            f"WIP metadata not found: persona={persona_id} name={set_name}"
        )

    # base 画像 (= idle は 01_face/face.png、 他 5 表情は 02_expressions/<expr>.png)。
    base_paths: dict[str, Path] = {}
    for face in ap.FACE_NAMES:
        if face == "idle":
            p = _stage_face_file(mgr, persona_id, set_name)
        else:
            p = _stage_expression_file(mgr, persona_id, set_name, face)
        if not p.exists():
            raise FileNotFoundError(
                f"Stage base image not found for face={face}: {p}"
            )
        base_paths[face] = p

    parallelism = max(1, int(meta.parallelism or 5))
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    # params:
    #   only_target: 1 枚だけ生成 (= 単発、 base はコピー対応)
    #   face_filter: 1 表情の 14 枚だけ生成 (= 全件を 6 分割するため)
    #   skip_existing: 既存 PNG はタスクから除外 (= 失敗後の resume、 金ドブ回避)
    only_target = params.get("only_target")
    face_filter = params.get("face_filter")
    skip_existing = bool(params.get("skip_existing", False))

    allowed_faces = (
        [face_filter] if face_filter in ap.FACE_NAMES
        else list(ap.FACE_NAMES)
    )

    if isinstance(only_target, str) and "_" in only_target:
        parts = only_target.split("_")
        if (
            len(parts) == 3
            and parts[0] in ap.FACE_NAMES
            and parts[1] in ap.EYES_STATES
            and parts[2] in ap.MOUTH_SHAPES
        ):
            tasks: list[tuple[str, str, str]] = []
            # base (open/closed) を選んでも生成しない (= コピー対応)。
            if parts[1] == "open" and parts[2] == "closed":
                # base = ②/① の画像をコピーして対応。
                src = base_paths[parts[0]]
                dst = _stage_matrix_file(
                    mgr, persona_id, set_name, *parts,
                )
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
                return {
                    "stage_id": ap.STAGE_MATRIX,
                    "files": [{
                        "target": only_target,
                        "path": str(dst),
                        "copied_from_base": True,
                    }],
                    "errors": [],
                }
            tasks.append(tuple(parts))
        else:
            tasks = []  # 不正な only_target は no-op (= 全件生成しない)
    else:
        # 通常: 生成タスク (= face_filter / skip_existing 適用後)。
        tasks = []
        for face in allowed_faces:
            for eyes in ap.EYES_STATES:
                for mouth in ap.MOUTH_SHAPES:
                    if eyes == "open" and mouth == "closed":
                        continue  # base はコピーで対応 (後段)
                    if skip_existing:
                        expected = _stage_matrix_file(
                            mgr, persona_id, set_name, face, eyes, mouth,
                        )
                        if expected.exists():
                            continue
                    tasks.append((face, eyes, mouth))

    matrix_aspect = _resolve_aspect_ratio(meta, ap.STAGE_MATRIX)
    matrix_quality = _resolve_quality(meta, ap.STAGE_MATRIX)
    constraint = _get_stage3_constraint_prompt(meta)
    common = _common_for_stage3(meta)
    eye_modify_hint = _get_eye_modify_hint(meta)

    def _do_one(face: str, eyes: str, mouth: str) -> tuple[bytes, str]:
        # ③ プロンプト構築の意図 (まはー検証 2026-05-17):
        # - common_prompt 不使用 (= 元画像にないアクセサリの差分追加防止)
        # - face は端的化 (= ②長文テンプレとの矛盾で 2 枚並べ事故防止)
        # - eyes=open / mouth=closed のセルは該当プロンプト省略
        #   (= 参照画像と同じ状態を「触る指示」 で AI に変更余地を渡さない)
        # - eyes != "open" のセルに eye_modify_hint 追加
        #   (= 参照画像が最大開き状態だと明示、 happy で half>open 事故防止)
        face_label = _stage3_face_label(face)
        extra_eyes = (
            _get_eyes_prompt(meta, eyes) if eyes != "open" else ""
        )
        extra_mouth = (
            _get_mouth_prompt(meta, mouth) if mouth != "closed" else ""
        )
        eye_hint = eye_modify_hint if eyes != "open" else ""
        per_target_key = f"{face}_{eyes}_{mouth}"
        extra_per_target = _get_extra_prompt(
            meta, ap.STAGE_MATRIX, per_target_key,
        )
        prompt = _build_prompt(
            constraint, common, face_label,
            eye_hint, extra_eyes, extra_mouth, extra_per_target,
        )
        return _generate_one(
            prompt=prompt,
            model=meta.image_model,
            input_image_paths=[base_paths[face]],
            aspect_ratio=matrix_aspect,
            quality=matrix_quality,
        )

    with ThreadPoolExecutor(max_workers=parallelism) as ex:
        futures = {
            ex.submit(_do_one, face, eyes, mouth): (face, eyes, mouth)
            for (face, eyes, mouth) in tasks
        }
        for fut in as_completed(futures):
            face, eyes, mouth = futures[fut]
            try:
                image_bytes, mime = fut.result()
                out_path = _stage_matrix_file(
                    mgr, persona_id, set_name, face, eyes, mouth,
                )
                _write_png(out_path, image_bytes, mime)
                results.append({
                    "target": f"{face}_{eyes}_{mouth}",
                    "path": str(out_path),
                })
            except Exception as exc:
                LOGGER.exception(
                    "avatar_generator: stage 03_matrix failed %s/%s/%s "
                    "persona=%s set=%s: %s",
                    face, eyes, mouth, persona_id, set_name, exc,
                )
                errors.append({
                    "target": f"{face}_{eyes}_{mouth}",
                    "error": str(exc),
                })

    # base = (eyes=open, mouth=closed) を copy。
    # 適用範囲: only_target 指定なし、 face_filter で絞られた face のみ。
    # skip_existing なら既存ファイルは copy しない。
    if not isinstance(only_target, str) or not only_target:
        for face in allowed_faces:
            src = base_paths[face]
            dst = _stage_matrix_file(
                mgr, persona_id, set_name, face, "open", "closed",
            )
            if skip_existing and dst.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            results.append({
                "target": f"{face}_open_closed",
                "path": str(dst),
                "copied_from_base": True,
            })

    base_copy_count = sum(
        1 for r in results if r.get("copied_from_base")
    )
    LOGGER.info(
        "avatar_generator: stage 03_matrix persona=%s set=%s "
        "ok=%d err=%d (incl %d base copies)",
        persona_id, set_name, len(results), len(errors),
        base_copy_count,
    )
    return {
        "stage_id": ap.STAGE_MATRIX,
        "files": results,
        "errors": errors,
    }


def generate_stage_layered(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """③ 目・口差分生成 (layered mode、 目 3 + 口 5 = 8 パーツ)。"""
    meta = mgr.read_metadata(persona_id, set_name)
    if meta is None:
        raise FileNotFoundError(
            f"WIP metadata not found: persona={persona_id} name={set_name}"
        )
    face_path = _stage_face_file(mgr, persona_id, set_name)
    if not face_path.exists():
        raise FileNotFoundError(
            f"Stage 01_face output not found: {face_path}"
        )

    parallelism = max(1, int(meta.parallelism or 5))
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    only_target = params.get("only_target")
    skip_existing = bool(params.get("skip_existing", False))
    # eyes=open と mouth=closed は ① 元画像 (face.png) を copy で対応
    # (= 参照画像と同じ状態、 AI に「触る指示」 を渡さないことで安定 +
    # コスト削減、 まはー検証 2026-05-17)。
    COPY_PAIRS = {("eyes", "open"), ("mouth", "closed")}
    tasks: list[tuple[str, str]] = []
    copy_tasks: list[tuple[str, str]] = []

    def _classify(kind: str, name: str) -> None:
        if (kind, name) in COPY_PAIRS:
            copy_tasks.append((kind, name))
        else:
            tasks.append((kind, name))

    if isinstance(only_target, str) and "_" in only_target:
        kind, name = only_target.split("_", 1)
        if (
            (kind == "eyes" and name in ap.EYES_STATES)
            or (kind == "mouth" and name in ap.MOUTH_SHAPES)
        ):
            _classify(kind, name)
    if not tasks and not copy_tasks:
        for eyes in ap.EYES_STATES:
            _classify("eyes", eyes)
        for mouth in ap.MOUTH_SHAPES:
            _classify("mouth", mouth)
    if skip_existing:
        tasks = [
            (k, n) for (k, n) in tasks
            if not _stage_layered_file(
                mgr, persona_id, set_name, k, n,
            ).exists()
        ]
        copy_tasks = [
            (k, n) for (k, n) in copy_tasks
            if not _stage_layered_file(
                mgr, persona_id, set_name, k, n,
            ).exists()
        ]

    layered_aspect = _resolve_aspect_ratio(meta, ap.STAGE_LAYERED)
    layered_quality = _resolve_quality(meta, ap.STAGE_LAYERED)
    constraint = _get_stage3_constraint_prompt(meta)
    common = _common_for_stage3(meta)
    eye_modify_hint = _get_eye_modify_hint(meta)

    def _do_one(kind: str, name: str) -> tuple[bytes, str]:
        # tasks には eyes=open / mouth=closed は入らない (= copy 経路)。
        # 残りは「目編集」 or「口編集」 のみ、 hint も対応して追加。
        if kind == "eyes":
            extra = _get_eyes_prompt(meta, name)
            eye_hint = eye_modify_hint  # eyes=open ではないので常時付与
        else:
            extra = _get_mouth_prompt(meta, name)
            eye_hint = ""
        per_target_key = f"{kind}_{name}"
        extra_per_target = _get_extra_prompt(
            meta, ap.STAGE_LAYERED, per_target_key,
        )
        prompt = _build_prompt(
            constraint, common, eye_hint, extra, extra_per_target,
        )
        return _generate_one(
            prompt=prompt,
            model=meta.image_model,
            input_image_paths=[face_path],
            aspect_ratio=layered_aspect,
            quality=layered_quality,
        )

    with ThreadPoolExecutor(max_workers=parallelism) as ex:
        futures = {
            ex.submit(_do_one, kind, name): (kind, name)
            for (kind, name) in tasks
        }
        for fut in as_completed(futures):
            kind, name = futures[fut]
            try:
                image_bytes, mime = fut.result()
                out_path = _stage_layered_file(
                    mgr, persona_id, set_name, kind, name,
                )
                _write_png(out_path, image_bytes, mime)
                results.append({
                    "target": f"{kind}_{name}",
                    "path": str(out_path),
                })
            except Exception as exc:
                LOGGER.exception(
                    "avatar_generator: stage 03_layered failed %s/%s "
                    "persona=%s set=%s: %s",
                    kind, name, persona_id, set_name, exc,
                )
                errors.append({
                    "target": f"{kind}_{name}",
                    "error": str(exc),
                })

    # copy_tasks (= eyes=open / mouth=closed) を ① face から copy。
    for (kind, name) in copy_tasks:
        dst = _stage_layered_file(mgr, persona_id, set_name, kind, name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(face_path, dst)
        results.append({
            "target": f"{kind}_{name}",
            "path": str(dst),
            "copied_from_base": True,
        })

    LOGGER.info(
        "avatar_generator: stage 03_layered persona=%s set=%s "
        "ok=%d err=%d (incl %d copies)",
        persona_id, set_name, len(results), len(errors), len(copy_tasks),
    )
    return {
        "stage_id": ap.STAGE_LAYERED,
        "files": results,
        "errors": errors,
    }


# ----- Hook dispatchers (= avatar_pipeline に register する) -----


def stage_executor(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
    stage_id: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """段階 ID で対応する生成関数にディスパッチ。 ④⑤⑥ は 4.5-d-3 で実装。"""
    if stage_id == ap.STAGE_FACE:
        return generate_stage_face(mgr, persona_id, set_name, params)
    if stage_id == ap.STAGE_EXPRESSIONS:
        return generate_stage_expressions(mgr, persona_id, set_name, params)
    if stage_id == ap.STAGE_MATRIX:
        return generate_stage_matrix(mgr, persona_id, set_name, params)
    if stage_id == ap.STAGE_LAYERED:
        return generate_stage_layered(mgr, persona_id, set_name, params)
    if stage_id == ap.STAGE_TRIMMED:
        # Phase 4.5-d-3 (avatar_finalizer) に委譲。
        from avatar_finalizer import generate_stage_trim
        return generate_stage_trim(mgr, persona_id, set_name, params)
    raise ValueError(f"Unknown stage_id: {stage_id!r}")


def regenerate_target(
    mgr: ap.AvatarPipelineManager,
    persona_id: str,
    set_name: str,
    stage_id: str,
    target: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """単発再生成。 target は段階内のファイル名 stem。

    params:
      extra_prompt: optional, 該当画像の追加自由文の上書き (= UI の編集
                    modal から)、 上書き時は metadata にも永続化
    """
    meta = mgr.read_metadata(persona_id, set_name)
    if meta is None:
        raise FileNotFoundError(
            f"WIP metadata not found: persona={persona_id} name={set_name}"
        )
    override = params.get("extra_prompt")
    override_text = override if isinstance(override, str) and override.strip() else None

    if stage_id == ap.STAGE_FACE:
        if target != "face":
            raise ValueError(
                f"01_face stage only has 'face' target, got {target!r}"
            )
        return generate_stage_face(
            mgr, persona_id, set_name,
            {"extra_prompt": override_text} if override_text else {},
        )

    if stage_id == ap.STAGE_EXPRESSIONS:
        if target not in ap.EXPRESSION_NAMES:
            raise ValueError(
                f"Unknown expression target: {target!r} "
                f"(allowed: {ap.EXPRESSION_NAMES})"
            )
        face_path = _stage_face_file(mgr, persona_id, set_name)
        if not face_path.exists():
            raise FileNotFoundError(f"Stage 01_face not found: {face_path}")
        extra = override_text or _get_extra_prompt(
            meta, ap.STAGE_EXPRESSIONS, target,
        )
        prompt = _build_prompt(meta.common_prompt, extra)
        image_bytes, mime = _generate_one(
            prompt=prompt, model=meta.image_model,
            input_image_paths=[face_path],
            aspect_ratio=_resolve_aspect_ratio(meta, ap.STAGE_EXPRESSIONS),
            quality=_resolve_quality(meta, ap.STAGE_EXPRESSIONS),
        )
        out_path = _stage_expression_file(
            mgr, persona_id, set_name, target,
        )
        _write_png(out_path, image_bytes, mime)
        if override_text:
            _persist_extra_prompt_override(
                mgr, persona_id, set_name,
                ap.STAGE_EXPRESSIONS, target, override_text,
            )
        return {
            "stage_id": stage_id, "target": target,
            "path": str(out_path),
        }

    if stage_id == ap.STAGE_MATRIX:
        parts = target.split("_")
        if len(parts) != 3:
            raise ValueError(
                f"Expected target format 'face_eyes_mouth', got {target!r}"
            )
        face, eyes, mouth = parts
        if face not in ap.FACE_NAMES:
            raise ValueError(f"Unknown face: {face!r}")
        if eyes not in ap.EYES_STATES:
            raise ValueError(f"Unknown eyes: {eyes!r}")
        if mouth not in ap.MOUTH_SHAPES:
            raise ValueError(f"Unknown mouth: {mouth!r}")
        if face == "idle":
            base_path = _stage_face_file(mgr, persona_id, set_name)
        else:
            base_path = _stage_expression_file(
                mgr, persona_id, set_name, face,
            )
        if not base_path.exists():
            raise FileNotFoundError(f"Base image not found: {base_path}")
        # base 自体は generate せず copy で再現。
        if eyes == "open" and mouth == "closed":
            out_path = _stage_matrix_file(
                mgr, persona_id, set_name, face, eyes, mouth,
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(base_path, out_path)
            return {
                "stage_id": stage_id, "target": target,
                "path": str(out_path),
                "copied_from_base": True,
            }
        # ③ matrix: 通常生成と同じプロンプト構築ロジック (= _do_one と整合):
        # eyes=open / mouth=closed プロンプト省略、 eyes!=open に eye_modify_hint。
        face_label = _stage3_face_label(face)
        extra_eyes = (
            _get_eyes_prompt(meta, eyes) if eyes != "open" else ""
        )
        extra_mouth = (
            _get_mouth_prompt(meta, mouth) if mouth != "closed" else ""
        )
        extra_per_target = override_text or _get_extra_prompt(
            meta, ap.STAGE_MATRIX, target,
        )
        constraint = _get_stage3_constraint_prompt(meta)
        common = _common_for_stage3(meta)
        eye_hint = (
            _get_eye_modify_hint(meta) if eyes != "open" else ""
        )
        prompt = _build_prompt(
            constraint, common, face_label,
            eye_hint, extra_eyes, extra_mouth, extra_per_target,
        )
        image_bytes, mime = _generate_one(
            prompt=prompt, model=meta.image_model,
            input_image_paths=[base_path],
            aspect_ratio=_resolve_aspect_ratio(meta, ap.STAGE_MATRIX),
            quality=_resolve_quality(meta, ap.STAGE_MATRIX),
        )
        out_path = _stage_matrix_file(
            mgr, persona_id, set_name, face, eyes, mouth,
        )
        _write_png(out_path, image_bytes, mime)
        if override_text:
            _persist_extra_prompt_override(
                mgr, persona_id, set_name,
                ap.STAGE_MATRIX, target, override_text,
            )
        return {
            "stage_id": stage_id, "target": target,
            "path": str(out_path),
        }

    if stage_id == ap.STAGE_LAYERED:
        if "_" not in target:
            raise ValueError(
                f"Expected target format 'eyes_<state>' or 'mouth_<shape>', "
                f"got {target!r}"
            )
        kind, name = target.split("_", 1)
        if kind not in ("eyes", "mouth"):
            raise ValueError(f"Unknown kind: {kind!r}")
        if kind == "eyes" and name not in ap.EYES_STATES:
            raise ValueError(f"Unknown eyes state: {name!r}")
        if kind == "mouth" and name not in ap.MOUTH_SHAPES:
            raise ValueError(f"Unknown mouth shape: {name!r}")
        face_path = _stage_face_file(mgr, persona_id, set_name)
        if not face_path.exists():
            raise FileNotFoundError(f"Stage 01_face not found: {face_path}")
        # eyes=open / mouth=closed は copy 対応 (= ① face と同じ状態、
        # AI に変更余地を渡さない、 まはー検証 2026-05-17)。
        is_copy_pair = (
            (kind == "eyes" and name == "open")
            or (kind == "mouth" and name == "closed")
        )
        out_path = _stage_layered_file(
            mgr, persona_id, set_name, kind, name,
        )
        if is_copy_pair:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(face_path, out_path)
            return {
                "stage_id": stage_id, "target": target,
                "path": str(out_path),
                "copied_from_base": True,
            }
        if kind == "eyes":
            extra = override_text or _get_eyes_prompt(meta, name)
        else:
            extra = override_text or _get_mouth_prompt(meta, name)
        per_target_extra = _get_extra_prompt(meta, ap.STAGE_LAYERED, target)
        # ③ layered: constraint 必須 prepend、 common は default OFF。
        # eyes != "open" のときだけ eye_modify_hint を追加。
        constraint = _get_stage3_constraint_prompt(meta)
        common = _common_for_stage3(meta)
        eye_hint = (
            _get_eye_modify_hint(meta)
            if kind == "eyes" and name != "open"
            else ""
        )
        prompt = _build_prompt(
            constraint, common, eye_hint, extra, per_target_extra,
        )
        image_bytes, mime = _generate_one(
            prompt=prompt, model=meta.image_model,
            input_image_paths=[face_path],
            aspect_ratio=_resolve_aspect_ratio(meta, ap.STAGE_LAYERED),
            quality=_resolve_quality(meta, ap.STAGE_LAYERED),
        )
        _write_png(out_path, image_bytes, mime)
        if override_text:
            _persist_extra_prompt_override(
                mgr, persona_id, set_name, kind, name, override_text,
            )
        return {
            "stage_id": stage_id, "target": target,
            "path": str(out_path),
        }

    raise ValueError(f"Cannot regenerate in stage_id: {stage_id!r}")


def register_avatar_executors(mgr: ap.AvatarPipelineManager) -> None:
    """avatar_pipeline manager に画像生成 executor を hook 注入する。

    api_routes.py の bootstrap で呼ばれる。
    """
    mgr.register_stage_executor(stage_executor)
    mgr.register_regenerate_executor(regenerate_target)
    LOGGER.info(
        "avatar_generator: executors registered to AvatarPipelineManager",
    )


__all__ = [
    "DEFAULT_TEMPLATES",
    "generate_stage_face",
    "generate_stage_expressions",
    "generate_stage_matrix",
    "generate_stage_layered",
    "stage_executor",
    "regenerate_target",
    "register_avatar_executors",
]
