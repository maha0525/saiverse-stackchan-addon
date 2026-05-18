"""Stack-chan Vessel addon の HTTP REST API (Phase 4.5-d 以降)。

addon_loader.load_addon_routers() により `/api/addon/saiverse-stackchan-addon/`
プレフィックスで自動 mount される。

Phase 4.5-d-1 (本ファイル): avatar セットの WIP 永続化と state 管理用の
endpoint を提供。 段階実行 (= 画像生成) と単発再生成は Phase 4.5-d-2 で
`avatar_pipeline.register_stage_executor()` 経由で hook 注入されるまでは
501 を返す。

Pydantic モデルで `from __future__ import annotations` を使うと
addon_loader の spec_from_file_location ロード経路で forward ref 解決が
壊れる (= memory feedback_addon_pydantic_future_annotations.md)。 本ファイル
では future annotations を使わないこと。
"""
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import (
    APIRouter, File, Form, HTTPException, Query, UploadFile, status,
)
from fastapi.responses import FileResponse
from pydantic import BaseModel

# 同梱モジュールを絶対 import するためにパック自身のディレクトリを
# sys.path に追加する (= avatar_loader.py 参照)。
_PACK_DIR = str(Path(__file__).parent)
if _PACK_DIR not in sys.path:
    sys.path.insert(0, _PACK_DIR)

from avatar_pipeline import (  # noqa: E402
    VALID_MODES,
    AvatarPipelineManager,
    get_avatar_pipeline_manager,
)

LOGGER = logging.getLogger(__name__)

router = APIRouter()


# ----- Request / Response schemas -----


class CreateSetRequest(BaseModel):
    mode: str = "matrix"
    common_prompt: str = ""
    image_model: str = "nano_banana_2"


class UpdateMetadataRequest(BaseModel):
    """metadata の任意フィールド更新。 未指定フィールドは変更しない。"""
    common_prompt: Optional[str] = None
    extra_prompts: Optional[dict] = None
    trim_rect: Optional[dict] = None
    trim_rect_overrides: Optional[dict] = None
    parallelism: Optional[int] = None
    image_model: Optional[str] = None
    current_stage: Optional[str] = None
    # Phase 4.5-d 追補: quality / aspect_ratio + 段階別 override (Debug 用)。
    image_quality: Optional[str] = None
    aspect_ratio: Optional[str] = None
    stage_quality_overrides: Optional[dict] = None
    stage_aspect_overrides: Optional[dict] = None
    apply_common_prompt_to_stage3: Optional[bool] = None


class SetActiveRequest(BaseModel):
    """`set_name=null` でクリア。"""
    set_name: Optional[str] = None


class StageExecuteRequest(BaseModel):
    """段階実行のパラメータ。 hook 側で解釈される (= Phase 4.5-d-2)。"""
    params: Optional[dict] = None


class RegenerateRequest(BaseModel):
    """単発再生成のパラメータ。"""
    target: str
    params: Optional[dict] = None


# ----- Endpoint helpers -----


def _mgr() -> AvatarPipelineManager:
    return get_avatar_pipeline_manager()


def _wrap_value_error(exc: ValueError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=str(exc),
    )


# OpenAI billing overview の URL (= 残高確認 + チャージ + limit 設定が
# 1 ページで完結する案内先)。
# `billing_hard_limit_reached` という error code 名は誤解を招くが、 実態は
# 「credit 残高切れ (= prepaid 0 以下)」 / 「設定済み hard limit に到達」 の
# どちらでも返ってくる。 prepaid デフォルト運用が増えた現在、 残高切れの
# 方が多い (まはー指摘 2026-05-17)。
_OPENAI_BILLING_URL = (
    "https://platform.openai.com/settings/organization/billing/overview"
)


def _unhandled(operation: str, exc: Exception) -> HTTPException:
    """予期しない例外 (= ValueError / FileNotFoundError 等以外) の共通処理。

    詳細を ERROR log + frontend には 500 で原因を含む detail を返す
    (= memory 「ロギングは実装時点で」、 「再現してログ追加」 を防ぐ)。

    特殊扱い: OpenAI の billing_hard_limit_reached は 402 (Payment Required)
    で返し、 frontend が chain 中に即 abort できるようにする (= 残り task
    でも同 error 繰り返すだけで意味なし)。
    """
    # OpenAI 等 SDK の構造化情報を抽出。
    parts: list[str] = [
        f"type={type(exc).__name__}", f"msg={exc!s}",
    ]
    for attr in ("status_code", "code", "type"):
        v = getattr(exc, attr, None)
        if v is not None:
            parts.append(f"{attr}={v}")
    body = getattr(exc, "body", None)
    if body is not None:
        parts.append(f"body={body!r}")
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            parts.append(f"response_text={response.text!r}")
        except Exception:
            parts.append(f"response_type={type(response).__name__}")
    detail = " ".join(parts)

    # OpenAI billing: chain 即停止のため 402 で返す。
    # billing_hard_limit_reached は「残高切れ (= prepaid 0 以下)」 / 「設定済み
    # hard limit 到達」 のどちらでも来る (= まはー検証 2026-05-17)。 残高切れ
    # の人の方が多い想定で billing overview に誘導 (= 残高確認 + チャージ +
    # limit 確認が 1 ページで完結)。
    exc_code = getattr(exc, "code", None)
    if exc_code == "billing_hard_limit_reached":
        LOGGER.warning(
            "api_routes: %s billing_hard_limit_reached - returning 402",
            operation,
        )
        return HTTPException(
            status_code=402,
            detail=(
                "OpenAI 課金上限到達 or credit 残高切れ。 "
                f"{_OPENAI_BILLING_URL} で残高 / limit を確認し、 必要なら "
                "チャージ or hard limit 引き上げ。 "
                "もしくは Debug 設定で image_model を別 backend に切替 "
                "(= nano_banana_2 / nano_banana_pro / grok_imagine)。"
            ),
        )
    # OpenAI insufficient_quota / rate_limit_exceeded も chain 続行は
    # 無意味なので 402 扱い (= chain abort trigger、 frontend で同じく扱う)。
    if exc_code in ("insufficient_quota", "rate_limit_exceeded"):
        LOGGER.warning(
            "api_routes: %s code=%s - returning 402 to abort chain",
            operation, exc_code,
        )
        return HTTPException(
            status_code=402,
            detail=(
                f"OpenAI API rejected: {exc_code}. "
                f"残高 / quota を {_OPENAI_BILLING_URL} で確認、 "
                "もしくは Debug 設定で別 backend に切替。"
            ),
        )

    LOGGER.exception(
        "api_routes: %s FAILED | %s", operation, detail,
    )
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail=f"{operation} failed: {detail}",
    )


# ----- Templates endpoint -----
# 注意: `/avatar_sets/templates` は `/avatar_sets/{persona_id}` よりも先に
# 定義 (= persona_id として "templates" が吸われるのを防ぐ、 同様に
# `/avatar_sets/{persona_id}/active` も後述の順序制約と同根)。


@router.get("/avatar_sets/templates")
def get_templates() -> dict:
    """初期プロンプトテンプレート (= UI で入力欄の初期値に使う)。"""
    from avatar_generator import DEFAULT_TEMPLATES
    return DEFAULT_TEMPLATES


# ----- Active set endpoints -----
# 注意: `/avatar_sets/{persona_id}/active` は `/{persona_id}/{set_name}` よりも
# 先に定義する必要がある (= FastAPI は登録順にマッチング、 先に汎用 path
# を登録すると "active" が set_name として吸われて 404 になる)。


@router.get("/avatar_sets/{persona_id}/active")
def get_active(persona_id: str) -> dict:
    try:
        active = _mgr().get_active(persona_id)
    except ValueError as exc:
        raise _wrap_value_error(exc)
    return {"persona_id": persona_id, "set_name": active}


@router.post("/avatar_sets/{persona_id}/active")
def set_active(persona_id: str, body: SetActiveRequest) -> dict:
    try:
        _mgr().set_active(persona_id, body.set_name)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        )
    except ValueError as exc:
        raise _wrap_value_error(exc)
    return {"persona_id": persona_id, "set_name": body.set_name}


# ----- Avatar set CRUD endpoints -----


@router.get("/avatar_sets/{persona_id}")
def list_sets(persona_id: str) -> dict:
    """ペルソナの全 avatar セット一覧 + アクティブセット名を返す。"""
    try:
        sets = _mgr().list_sets(persona_id)
        active = _mgr().get_active(persona_id)
    except ValueError as exc:
        raise _wrap_value_error(exc)
    return {
        "persona_id": persona_id,
        "active_set_name": active,
        "sets": [s.to_json() for s in sets],
    }


@router.post(
    "/avatar_sets/{persona_id}/{set_name}",
    status_code=status.HTTP_201_CREATED,
)
def create_set(
    persona_id: str,
    set_name: str,
    body: CreateSetRequest,
) -> dict:
    """新規セットを作成 (= WIP のみ、 確定品はまだ無い)。"""
    if body.mode not in VALID_MODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid mode: {body.mode!r} (allowed: {list(VALID_MODES)})",
        )
    try:
        info = _mgr().create_set(
            persona_id=persona_id,
            set_name=set_name,
            mode=body.mode,
            common_prompt=body.common_prompt,
            image_model=body.image_model,
        )
    except ValueError as exc:
        # 既存衝突は 409、 それ以外の validation エラーは 400。
        msg = str(exc)
        if "already exists" in msg:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=msg,
            )
        raise _wrap_value_error(exc)
    return info.to_json()


@router.get("/avatar_sets/{persona_id}/{set_name}")
def get_set(persona_id: str, set_name: str) -> dict:
    """単一セットの状態 (= 確定品 + WIP) を取得。"""
    try:
        info = _mgr().get_set(persona_id, set_name)
    except ValueError as exc:
        raise _wrap_value_error(exc)
    if info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Set not found: persona={persona_id} name={set_name}",
        )
    return info.to_json()


@router.delete("/avatar_sets/{persona_id}/{set_name}")
def delete_set(
    persona_id: str,
    set_name: str,
    wip_only: bool = Query(False, description="True で wip/ のみ削除"),
) -> dict:
    """セット削除。 `wip_only=true` なら確定品を残して WIP のみ削除。"""
    try:
        deleted = _mgr().delete_set(
            persona_id, set_name, wip_only=wip_only,
        )
    except ValueError as exc:
        raise _wrap_value_error(exc)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Nothing to delete: persona={persona_id} name={set_name} "
                f"wip_only={wip_only}"
            ),
        )
    return {"deleted": True, "wip_only": wip_only}


# ----- Metadata endpoints -----


@router.patch("/avatar_sets/{persona_id}/{set_name}/metadata")
def update_metadata(
    persona_id: str,
    set_name: str,
    body: UpdateMetadataRequest,
) -> dict:
    """metadata.json の特定フィールド更新 (= 共通プロンプト / 追加自由文 /
    トリミング矩形 / 並列度 / モデル / current_stage)。"""
    updates = {
        k: v for k, v in body.model_dump(exclude_unset=True).items()
        if v is not None
    }
    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )
    try:
        meta = _mgr().update_metadata(persona_id, set_name, **updates)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        )
    except ValueError as exc:
        raise _wrap_value_error(exc)
    return meta.to_json()


# ----- Stage endpoints -----


@router.post("/avatar_sets/{persona_id}/{set_name}/stages/{stage_id}/complete")
def complete_stage(persona_id: str, set_name: str, stage_id: str) -> dict:
    """段階完了マーク (= ユーザーが「次へ」を押した時)。"""
    try:
        meta = _mgr().mark_stage_completed(persona_id, set_name, stage_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        )
    except ValueError as exc:
        raise _wrap_value_error(exc)
    return meta.to_json()


@router.post("/avatar_sets/{persona_id}/{set_name}/stages/{stage_id}")
def execute_stage(
    persona_id: str,
    set_name: str,
    stage_id: str,
    body: StageExecuteRequest,
) -> dict:
    """段階実行を hook に委譲。 Phase 4.5-d-2 で hook 注入されるまで 501。"""
    LOGGER.info(
        "api_routes: execute_stage RECV persona=%s set=%s stage=%s params=%r",
        persona_id, set_name, stage_id, body.params,
    )
    op = (
        f"execute_stage(persona={persona_id}, set={set_name}, "
        f"stage={stage_id})"
    )
    try:
        result = _mgr().execute_stage(
            persona_id, set_name, stage_id, params=body.params or {},
        )
    except NotImplementedError as exc:
        LOGGER.warning("api_routes: %s NOT_IMPLEMENTED: %s", op, exc)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc),
        )
    except FileNotFoundError as exc:
        LOGGER.warning("api_routes: %s NOT_FOUND: %s", op, exc)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        )
    except ValueError as exc:
        LOGGER.warning("api_routes: %s BAD_REQUEST: %s", op, exc)
        raise _wrap_value_error(exc)
    except Exception as exc:
        raise _unhandled(op, exc)
    LOGGER.info(
        "api_routes: execute_stage OK persona=%s set=%s stage=%s "
        "files=%d errors=%d",
        persona_id, set_name, stage_id,
        len(result.get("files", []) if isinstance(result, dict) else []),
        len(result.get("errors", []) if isinstance(result, dict) else []),
    )
    return result


@router.post(
    "/avatar_sets/{persona_id}/{set_name}/stages/{stage_id}/regenerate"
)
def regenerate_target(
    persona_id: str,
    set_name: str,
    stage_id: str,
    body: RegenerateRequest,
) -> dict:
    """単発再生成を hook に委譲。 Phase 4.5-d-2 で hook 注入されるまで 501。"""
    LOGGER.info(
        "api_routes: regenerate RECV persona=%s set=%s stage=%s "
        "target=%s params=%r",
        persona_id, set_name, stage_id, body.target, body.params,
    )
    op = (
        f"regenerate(persona={persona_id}, set={set_name}, "
        f"stage={stage_id}, target={body.target})"
    )
    try:
        result = _mgr().regenerate_target(
            persona_id, set_name, stage_id, body.target,
            params=body.params or {},
        )
    except NotImplementedError as exc:
        LOGGER.warning("api_routes: %s NOT_IMPLEMENTED: %s", op, exc)
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc),
        )
    except FileNotFoundError as exc:
        LOGGER.warning("api_routes: %s NOT_FOUND: %s", op, exc)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        )
    except ValueError as exc:
        LOGGER.warning("api_routes: %s BAD_REQUEST: %s", op, exc)
        raise _wrap_value_error(exc)
    except Exception as exc:
        raise _unhandled(op, exc)
    LOGGER.info(
        "api_routes: regenerate OK persona=%s set=%s stage=%s target=%s",
        persona_id, set_name, stage_id, body.target,
    )
    return result


# ----- Image preview endpoint (Phase 4.5-d-4 UI 用) -----
#
# 各段階の生成画像 (PNG) を frontend が <img src=...> で取れるように raw を
# 返す。 path traversal を防ぐため stage_id と filename を厳格に validate。


_ALLOWED_STAGES_FOR_IMAGE = {
    "01_face", "02_expressions", "03_matrix", "03_layered", "04_trimmed",
}


@router.get(
    "/avatar_sets/{persona_id}/{set_name}/files/{stage_id}/{filename}"
)
def get_stage_image(
    persona_id: str, set_name: str, stage_id: str, filename: str,
) -> FileResponse:
    """WIP 段階画像 (PNG) の raw 配信。"""
    if stage_id not in _ALLOWED_STAGES_FOR_IMAGE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid stage_id: {stage_id!r}",
        )
    # filename には path 区切り / 親参照を含めない。 .png のみ許容。
    if (
        "/" in filename or "\\" in filename or ".." in filename
        or not filename.endswith(".png")
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid filename: {filename!r}",
        )
    try:
        stage_dir = _mgr().stage_dir(persona_id, set_name, stage_id)
    except ValueError as exc:
        raise _wrap_value_error(exc)
    file_path = stage_dir / filename
    if not file_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {filename}",
        )
    # Cache-Control: no-store で再生成後の差し替えがすぐ反映されるように。
    return FileResponse(
        file_path,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


# ----- Finalize / Transfer endpoints (Phase 4.5-d-3) -----
#
# ⑤ finalize と ⑥ transfer は WIP の段階扱いではなく、 確定品操作なので
# `/stages/...` ではなく専用 endpoint で公開。


@router.post("/avatar_sets/{persona_id}/{set_name}/finalize")
def finalize_set(persona_id: str, set_name: str) -> dict:
    """⑤ WIP 04_trimmed/ → avatar.bin + manifest.json 書き出し。"""
    op = f"finalize(persona={persona_id}, set={set_name})"
    LOGGER.info("api_routes: %s RECV", op)
    try:
        from avatar_finalizer import finalize_avatar_set
        result = finalize_avatar_set(_mgr(), persona_id, set_name)
    except FileNotFoundError as exc:
        LOGGER.warning("api_routes: %s NOT_FOUND: %s", op, exc)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        )
    except ValueError as exc:
        LOGGER.warning("api_routes: %s BAD_REQUEST: %s", op, exc)
        raise _wrap_value_error(exc)
    except RuntimeError as exc:
        LOGGER.exception("api_routes: %s RUNTIME_ERROR: %s", op, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc),
        )
    except Exception as exc:
        raise _unhandled(op, exc)
    LOGGER.info(
        "api_routes: %s OK bytes=%s checksum=%s",
        op, result.get("bytes"), result.get("checksum"),
    )
    return result


@router.post("/avatar_sets/{persona_id}/{set_name}/transfer")
def transfer_set(persona_id: str, set_name: str) -> dict:
    """⑥ 確定品を Stack-chan device に転送。 Vessel ペアリング + gateway
    接続が前提。"""
    op = f"transfer(persona={persona_id}, set={set_name})"
    LOGGER.info("api_routes: %s RECV", op)
    try:
        from avatar_finalizer import transfer_avatar_set
        result = transfer_avatar_set(_mgr(), persona_id, set_name)
    except FileNotFoundError as exc:
        LOGGER.warning("api_routes: %s NOT_FOUND: %s", op, exc)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        )
    except ValueError as exc:
        LOGGER.warning("api_routes: %s BAD_REQUEST: %s", op, exc)
        raise _wrap_value_error(exc)
    except RuntimeError as exc:
        LOGGER.exception("api_routes: %s RUNTIME_ERROR: %s", op, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc),
        )
    except Exception as exc:
        raise _unhandled(op, exc)
    LOGGER.info(
        "api_routes: %s OK result=%s", op, result.get("result"),
    )
    return result


# ----- ① 手動アップロード経路 (Phase 4.5-d 追補) -----


@router.post(
    "/avatar_sets/{persona_id}/{set_name}/stages/01_face/upload"
)
async def upload_face(
    persona_id: str,
    set_name: str,
    file: UploadFile = File(...),
    target_aspect: str = Form(
        ...,
        description="target アス比 (1:1 / 4:3 / 3:4 / 16:9 等)。 "
                    "metadata.aspect_ratio もこの値に揃う",
    ),
    crop_x: Optional[int] = Form(None),
    crop_y: Optional[int] = Form(None),
    crop_width: Optional[int] = Form(None),
    crop_height: Optional[int] = Form(None),
) -> dict:
    """① の元顔として既存画像をアップロードする経路。

    生成 (= POST /stages/01_face) とは独立。 ペルソナの標準顔画像が
    すでに用意されているケースで使う。 アップロード画像は target_aspect
    に合わせてクロップ (= crop_* 指定なしなら中央クロップ) されて
    `wip/01_face/face.png` に保存される。
    """
    contents = await file.read()
    if not contents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty upload",
        )
    crop_rect: Optional[dict] = None
    if (
        crop_x is not None and crop_y is not None
        and crop_width is not None and crop_height is not None
    ):
        crop_rect = {
            "x": crop_x, "y": crop_y,
            "width": crop_width, "height": crop_height,
        }
    op = (
        f"upload_face(persona={persona_id}, set={set_name}, "
        f"target_aspect={target_aspect}, crop={crop_rect}, "
        f"bytes={len(contents)})"
    )
    LOGGER.info("api_routes: %s RECV", op)
    try:
        from avatar_finalizer import upload_face_image
        result = upload_face_image(
            _mgr(), persona_id, set_name, contents,
            target_aspect=target_aspect,
            crop_rect=crop_rect,
        )
    except FileNotFoundError as exc:
        LOGGER.warning("api_routes: %s NOT_FOUND: %s", op, exc)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        )
    except ValueError as exc:
        LOGGER.warning("api_routes: %s BAD_REQUEST: %s", op, exc)
        raise _wrap_value_error(exc)
    except Exception as exc:
        raise _unhandled(op, exc)
    LOGGER.info("api_routes: %s OK path=%s", op, result.get("path"))
    return result


@router.post(
    "/avatar_sets/{persona_id}/{set_name}/stages/01_face/ref_image"
)
async def upload_ref_image(
    persona_id: str,
    set_name: str,
    file: UploadFile = File(...),
) -> dict:
    """① 生成経路で使う参照画像を WIP 内に保存。

    返り値の `path` を generate_stage_face の params.ref_image_paths に
    渡せば、 AI は参照画像を入力に取って生成する。
    """
    contents = await file.read()
    if not contents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty upload",
        )
    op = (
        f"upload_ref_image(persona={persona_id}, set={set_name}, "
        f"filename={file.filename!r}, bytes={len(contents)})"
    )
    LOGGER.info("api_routes: %s RECV", op)
    try:
        from avatar_finalizer import upload_reference_image
        result = upload_reference_image(
            _mgr(), persona_id, set_name, contents,
            filename_hint=file.filename or "ref.png",
        )
    except FileNotFoundError as exc:
        LOGGER.warning("api_routes: %s NOT_FOUND: %s", op, exc)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        )
    except ValueError as exc:
        LOGGER.warning("api_routes: %s BAD_REQUEST: %s", op, exc)
        raise _wrap_value_error(exc)
    except Exception as exc:
        raise _unhandled(op, exc)
    LOGGER.info("api_routes: %s OK path=%s", op, result.get("path"))
    return result


@router.get(
    "/avatar_sets/{persona_id}/{set_name}/stages/01_face/ref_images"
)
def list_ref_images(persona_id: str, set_name: str) -> dict:
    """保存済み参照画像の一覧。"""
    try:
        from avatar_finalizer import list_reference_images
        return {"refs": list_reference_images(_mgr(), persona_id, set_name)}
    except ValueError as exc:
        raise _wrap_value_error(exc)


@router.delete(
    "/avatar_sets/{persona_id}/{set_name}/stages/01_face/ref_images/{name}"
)
def delete_ref_image(persona_id: str, set_name: str, name: str) -> dict:
    """参照画像 1 個を削除。"""
    try:
        from avatar_finalizer import delete_reference_image
        deleted = delete_reference_image(
            _mgr(), persona_id, set_name, name,
        )
    except ValueError as exc:
        raise _wrap_value_error(exc)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Not found: {name}",
        )
    return {"deleted": True, "name": name}


@router.post("/avatar_sets/stages/01_face/analyze")
async def analyze_face_image(
    file: UploadFile = File(...),
) -> dict:
    """① upload 前のプレビュー用: 画像サイズ + 推奨アス比を返す。

    persona_id / set_name は不要 (= サーバー側に保存しない、 解析のみ)。
    """
    contents = await file.read()
    if not contents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty upload",
        )
    try:
        from avatar_finalizer import analyze_image
        return analyze_image(contents)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to analyze image: {exc}",
        )


# ----- Zip import endpoint (Phase 4.5-d-5) -----


@router.post("/avatar_sets/{persona_id}/{set_name}/import_zip")
async def import_zip(
    persona_id: str,
    set_name: str,
    file: UploadFile = File(...),
    require_complete: bool = Query(
        True,
        description="True なら mode に対応する全ファイルが zip にあることを要求",
    ),
) -> dict:
    """zip ファイルから ④ 04_trimmed/ に直接展開する経路。

    `④ から開始` 経路 (= ①②③ をスキップして手持ち画像を投入)。
    """
    contents = await file.read()
    if not contents:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Empty upload",
        )
    op = (
        f"import_zip(persona={persona_id}, set={set_name}, "
        f"bytes={len(contents)}, require_complete={require_complete})"
    )
    LOGGER.info("api_routes: %s RECV", op)
    try:
        from avatar_finalizer import import_trimmed_zip
        result = import_trimmed_zip(
            _mgr(), persona_id, set_name, contents,
            require_complete=require_complete,
        )
    except FileNotFoundError as exc:
        LOGGER.warning("api_routes: %s NOT_FOUND: %s", op, exc)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc),
        )
    except ValueError as exc:
        LOGGER.warning("api_routes: %s BAD_REQUEST: %s", op, exc)
        raise _wrap_value_error(exc)
    except Exception as exc:
        raise _unhandled(op, exc)
    LOGGER.info(
        "api_routes: %s OK extracted=%s missing=%s",
        op, result.get("extracted"), len(result.get("missing", []) or []),
    )
    return result


# ----- Bootstrap: register avatar generator executors -----
#
# api_routes.py が import される時点で 1 回だけ register する。 register_*
# は manager のインスタンス属性を更新するだけなので、 副作用は manager
# singleton にしか及ばない (= テスト時に singleton リセットすれば消える)。
# import 失敗時は WARNING に留めて起動を続行 (= 4.5-d-2 の生成機能だけが
# 死ぬ、 vessel ペアリングや avatar セット転送は影響なし)。

def _bootstrap_executors() -> None:
    try:
        from avatar_generator import register_avatar_executors
        register_avatar_executors(_mgr())
    except Exception:
        LOGGER.exception(
            "api_routes: failed to bootstrap avatar generator executors "
            "(画像生成 endpoint は 501 を返す状態になる)",
        )


# ----- Device control endpoints (Phase 4.5-f: addon UI からの直接操作) -----
#
# stackchan-mcp gateway 経由でデバイス状態の取得 / 制御を行う。 ペルソナ
# が spell で呼ぶ経路 (= LLM ツール呼び出し) とは別で、 ユーザーが Addon
# Panel UI から直接叩く用。 音量スライダ初期値取得 + 音量変更 + LED 消灯
# の 3 系統。
#
# 設計判断:
#   - sync def + threadpool 実行 (FastAPI 標準)。 内部で MCP loop へ
#     asyncio.run_coroutine_threadsafe で bridge する (= avatar_loader.py
#     と同じ pattern、 ただし向こうは ThreadPoolExecutor worker から呼ば
#     れるのに対しこちらは FastAPI threadpool)。
#   - MCP 未起動 / gateway 接続なし は 503 で返す (= UI 側で "Vessel
#     gateway が起きていない" を表示できるように)。

from avatar_loader import MCP_QUALIFIED_SERVER  # noqa: E402

_DEVICE_CALL_TIMEOUT_SEC = 5.0


def _call_device_mcp_tool(tool_name: str, args: dict) -> str:
    """stackchan MCP tool を 1 回呼んで text 結果を返す (sync helper)。

    FastAPI の sync endpoint から呼ばれる。 内部で MCP event loop に coro
    を投げる。
    """
    from tools.mcp_client import (  # type: ignore
        _make_instance_key, get_mcp_manager,
    )
    import tools.mcp_client as _mcp  # type: ignore

    manager = get_mcp_manager()
    if manager is None:
        raise HTTPException(
            status_code=503,
            detail="MCP manager not initialized",
        )
    instance_key = _make_instance_key(MCP_QUALIFIED_SERVER, persona_id=None)
    conn = manager._connections.get(instance_key)
    if conn is None:
        raise HTTPException(
            status_code=503,
            detail=(
                f"MCP server '{MCP_QUALIFIED_SERVER}' not connected "
                "(Vessel addon gateway 未起動か device 未ペアリング)"
            ),
        )
    loop = _mcp._loop
    if loop is None:
        raise HTTPException(
            status_code=503,
            detail="MCP event loop not initialized",
        )

    async def _do() -> str:
        return await conn.call_tool(tool_name, args)

    future = asyncio.run_coroutine_threadsafe(_do(), loop)
    try:
        return future.result(timeout=_DEVICE_CALL_TIMEOUT_SEC)
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail=(
                f"{tool_name} timed out after {_DEVICE_CALL_TIMEOUT_SEC}s "
                "(device 応答なし / WS 切断中?)"
            ),
        ) from exc
    except HTTPException:
        raise
    except Exception as exc:
        LOGGER.exception("device MCP call %s failed", tool_name)
        raise HTTPException(
            status_code=500,
            detail=f"{tool_name} failed: {type(exc).__name__}: {exc}",
        ) from exc


def _parse_mcp_text_as_dict(raw: Any) -> dict:
    """MCP tool の text 結果を dict に parse (失敗時は raw を載せて返す)。

    gateway 側は ESP32 からの JSON text をそのまま透過するので、
    通常は `{"volume": 50, "battery_level": 80, ...}` 形式の dict。
    firmware の予期せぬ仕様変更で非 JSON になっても 500 を返さず、
    UI 側で取得失敗を表示できるよう raw を返却する。
    """
    if not isinstance(raw, str):
        if isinstance(raw, dict):
            return raw
        return {"raw": str(raw)}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        LOGGER.warning(
            "device status: non-JSON MCP response (raw=%r)", raw,
        )
        return {"raw": raw}
    if not isinstance(parsed, dict):
        return {"raw": raw}
    return parsed


class SetDeviceVolumeRequest(BaseModel):
    """音量 (0-100)。 stackchan-mcp の `set_volume` schema と一致。"""
    volume: int


@router.get("/device/status")
def get_device_status() -> dict:
    """ｽﾀｯｸﾁｬﾝ device の現在状態 (volume / battery / WiFi 等) を取得。

    Panel.tsx のマウント時に 1 回呼び出して音量スライダの初期値に使う。
    継続的な polling は想定していない。
    """
    raw = _call_device_mcp_tool("get_device_info", {})
    return _parse_mcp_text_as_dict(raw)


@router.post("/device/volume")
def set_device_volume(req: SetDeviceVolumeRequest) -> dict:
    """ｽﾀｯｸﾁｬﾝ内部スピーカー音量を設定 (0-100)。"""
    if not 0 <= req.volume <= 100:
        raise HTTPException(
            status_code=400,
            detail=f"volume must be 0..100, got {req.volume}",
        )
    _call_device_mcp_tool("set_volume", {"volume": req.volume})
    LOGGER.info("device: set_volume %d", req.volume)
    return {"ok": True, "volume": req.volume}


@router.post("/device/leds/clear")
def clear_device_leds() -> dict:
    """ｽﾀｯｸﾁｬﾝ base RGB LED (12 個) を全消灯。"""
    _call_device_mcp_tool("clear_leds", {})
    LOGGER.info("device: clear_leds")
    return {"ok": True}


_bootstrap_executors()


# --- Audio input relay (v0.7: device-driven listen capture) -----------------
#
# stackchan-mcp gateway が device 主導 listen (LCD タッチ / ウェイクワード)
# の Opus 音声を Ogg コンテナにパックして POST してくる経路。詳細は
# audio_input_relay.py の docstring 参照。
from audio_input_relay import audio_router  # noqa: E402

router.include_router(audio_router)


__all__ = ["router"]
