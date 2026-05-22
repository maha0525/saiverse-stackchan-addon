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
    APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status,
)
from fastapi.responses import FileResponse, StreamingResponse
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


# ============================================================================
# Vessel Pairing endpoints (Phase 2')
# ============================================================================
# Stack-chan device の登録・解除を AddonManager UI から実行するための HTTP API。
# archive/api_routes.py から移植 (v0.5 Bearer Token モデル合わせ + AddonConfig
# 自動更新を追加)。
#
# 設計判断:
#   - Phase 1' の single vessel 前提に従い、既にペアリング済み vessel があれば
#     POST /pair は 409 を返す。ユーザーは DELETE で先に解除する
#   - AddonConfig.master_token / vessel_building_id はペアリング時に自動更新
#     (intent doc §Phase 2' 引き継ぎ事項、二重管理の自動同期)
#   - 解除時に AddonConfig はクリアしない (= 再ペアリング時に上書きされる、
#     UX で入力欄が空になると不便)
#   - WebSocket /vessel と firmware 配信は廃止 (gateway は stackchan-mcp、
#     flash は Step 4 で UI 経由 esptool subprocess に置き換え)

from saiverse.addon_deps import get_manager  # noqa: E402
from vessel_manager import get_vessel_manager  # noqa: E402

_ADDON_NAME_FOR_CONFIG = "saiverse-stackchan-addon"


class PairRequest(BaseModel):
    """新規ペアリング発行リクエスト。"""
    building_id: str
    persona_id: Optional[str] = None
    hardware_model: str = "stackchan_kickstarter_2025"


class PairResponse(BaseModel):
    """ペアリング発行レスポンス。device_token は平文で 1 回だけ返す。"""
    vessel_id: str
    device_token: str  # device の AP モード設定 UI に入力する値
    building_id: str
    gateway_ws_url: str  # ws://<vision_host>:<gateway_ws_port>/ (= AddonConfig 経由)


def _build_gateway_ws_url() -> str:
    """device の AP モード設定 UI で入力する Gateway URL を組み立てる。

    AddonConfig から ``vision_host`` (= LAN IP) と ``gateway_ws_port``
    (= WS 待受 port) を読んで合成する。 未設定なら placeholder 文字列を
    返して UI に「ここを埋めてね」 と気づかせる。
    """
    from saiverse.addon_config import get_params

    params = get_params(_ADDON_NAME_FOR_CONFIG)
    host = (params.get("vision_host") or "").strip() or "<vision_host 未設定>"
    port = (params.get("gateway_ws_port") or "").strip() or "8765"
    return f"ws://{host}:{port}/"


class VesselSummary(BaseModel):
    """vessel 一覧 entry。"""
    vessel_id: str
    bound_building_id: str
    bound_persona_id: Optional[str]
    hardware_model: str
    firmware_version: Optional[str]
    paired_at: str
    last_seen_at: Optional[str]
    connected: bool


def _update_addon_config_after_pair(
    db,
    *,
    master_token: str,
    vessel_building_id: str,
) -> None:
    """AddonConfig.params_json の master_token / vessel_building_id を更新。

    intent doc §Phase 2' 引き継ぎ事項に従い、ペアリング操作時に AddonConfig
    の値も自動で同期する (= mcp_servers.json placeholder の解決経路と
    vessel_manager の vessels.db の二重管理を解消)。

    既存パターン (api/routes/addon.py:440-445) と同じく AddonConfig 行を
    直接 update する。本体側に汎用 set_param() は追加しない (= addon 個別
    の都合で本体機構を生やすのを避ける、必要が出てきたら汎用化)。
    """
    from database.models import AddonConfig

    row = db.query(AddonConfig).filter_by(
        addon_name=_ADDON_NAME_FOR_CONFIG,
    ).first()
    if row is None:
        row = AddonConfig(
            addon_name=_ADDON_NAME_FOR_CONFIG,
            is_enabled=True,
            params_json=None,
        )
        db.add(row)

    existing: dict = {}
    if row.params_json:
        try:
            existing = json.loads(row.params_json)
        except (json.JSONDecodeError, TypeError):
            LOGGER.warning(
                "api_routes: invalid params_json for %s, treating as empty",
                _ADDON_NAME_FOR_CONFIG,
            )
            existing = {}

    existing["master_token"] = master_token
    existing["vessel_building_id"] = vessel_building_id
    row.params_json = json.dumps(existing, ensure_ascii=False)


@router.post("/pair", response_model=PairResponse)
def pair_vessel(
    req: PairRequest, manager=Depends(get_manager),
) -> PairResponse:
    """新規ペアリング発行 (Phase 2')。

    1. Phase 1' single vessel 前提: 既に vessel が登録されていれば 409 を返す
    2. Building 存在確認 + PHYSICAL_VESSEL_ID 未割り当て確認
    3. vessel_manager.create_pairing で vessel_id + device_token 発行
    4. Building.PHYSICAL_VESSEL_ID + CAPACITY=1 (不変条件 2) 強制
    5. AddonConfig.master_token / vessel_building_id 自動更新

    device_token は平文で 1 回だけレスポンスに含まれる。DB には sha256 ハッシュ
    のみが保存されるため、紛失時は再ペアリングが必要。
    """
    from database.models import Building

    vm = get_vessel_manager()

    # Phase 1' single vessel 前提
    existing = vm.list_vessels()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Already paired with vessel '{existing[0].vessel_id}'. "
                "Phase 1' は single vessel 前提、 先に DELETE /vessels/<id> で "
                "既存ペアリングを解除してから再実行してください。"
            ),
        )

    db = manager.SessionLocal()
    try:
        building = db.query(Building).filter_by(
            BUILDINGID=req.building_id,
        ).first()
        if not building:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Building '{req.building_id}' not found",
            )
        if building.PHYSICAL_VESSEL_ID:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Building '{req.building_id}' is already paired with "
                    f"vessel '{building.PHYSICAL_VESSEL_ID}'."
                ),
            )

        vessel_id, device_token = vm.create_pairing(
            building_id=req.building_id,
            persona_id=req.persona_id,
            hardware_model=req.hardware_model,
        )

        # 不変条件 2: Vessel Building は capacity=1 強制
        building.PHYSICAL_VESSEL_ID = vessel_id
        building.CAPACITY = 1

        _update_addon_config_after_pair(
            db,
            master_token=device_token,
            vessel_building_id=req.building_id,
        )

        db.commit()
        LOGGER.info(
            "pair_vessel: vessel_id=%s building_id=%s persona_id=%s "
            "(AddonConfig master_token / vessel_building_id auto-updated)",
            vessel_id, req.building_id, req.persona_id,
        )
    finally:
        db.close()

    # AddonConfig 更新が DB に commit された後、 stackchan-mcp gateway
    # subprocess を新 env で再起動する。 OS プロセスの env は起動時に固定
    # されるため、 master_token 変更を実 gateway に反映するには subprocess
    # 再起動が必須 (= さもなければ device は新 token で接続するが gateway
    # は古い token で 401 reject の連発になる)。
    #
    # tools.mcp_client.reconnect_mcp_server は内部で _server_meta の
    # raw_config を re-resolve してから disconnect → connect する (= 改修
    # 済み、 docs/intent/stackchan_vessel.md Phase 2' 改修参照)。
    _reconnect_stackchan_mcp_or_log()

    return PairResponse(
        vessel_id=vessel_id,
        device_token=device_token,
        building_id=req.building_id,
        gateway_ws_url=_build_gateway_ws_url(),
    )


_STACKCHAN_MCP_QUALIFIED_NAME = "saiverse-stackchan-addon__stackchan"


def _reconnect_stackchan_mcp_or_log() -> None:
    """ペアリング後に stackchan-mcp gateway を新 env で再起動する。

    失敗してもペアリング API は成功扱い (= vessels.db / Building /
    AddonConfig は既に commit 済み)。 reconnect 失敗時は WARNING ログを
    出して、 ユーザーには「SAIVerse 再起動で復旧する」 旨を UI で案内
    する余地を残す (= Step 5 で UI 側にも反映予定)。
    """
    from tools.mcp_client import (  # noqa: E402
        get_mcp_manager,
        reconnect_mcp_server,
    )
    import tools.mcp_client as _mcp  # noqa: E402

    manager = get_mcp_manager()
    if manager is None:
        LOGGER.warning(
            "pair_vessel: MCP manager not initialized, skipping gateway "
            "reconnect (= SAIVerse 再起動で復旧)",
        )
        return
    loop = _mcp._loop
    if loop is None:
        LOGGER.warning(
            "pair_vessel: MCP event loop not initialized, skipping gateway "
            "reconnect (= SAIVerse 再起動で復旧)",
        )
        return

    try:
        future = asyncio.run_coroutine_threadsafe(
            reconnect_mcp_server(_STACKCHAN_MCP_QUALIFIED_NAME),
            loop,
        )
        success = future.result(timeout=15.0)
        if success:
            LOGGER.info(
                "pair_vessel: stackchan-mcp gateway reconnected with new env",
            )
        else:
            LOGGER.warning(
                "pair_vessel: reconnect_mcp_server returned False "
                "(server '%s' may not be running, or env unchanged) "
                "— SAIVerse 再起動で確実に反映",
                _STACKCHAN_MCP_QUALIFIED_NAME,
            )
    except asyncio.TimeoutError:
        LOGGER.warning(
            "pair_vessel: gateway reconnect timed out after 15s "
            "(= subprocess respawn 中? SAIVerse 再起動で復旧)",
        )
    except Exception:
        LOGGER.exception(
            "pair_vessel: unexpected error during gateway reconnect "
            "(= ペアリング自体は成功、 SAIVerse 再起動で復旧)",
        )


@router.get("/vessels")
def list_vessels() -> dict:
    """登録済み vessel 一覧と接続状態を返す。

    `connected` は addon 内部の VesselSession state holder ベース。Phase 2'
    では Phase 3' / 5' の event 経路がまだ薄いので、 register_session を
    呼ぶ箇所が少なく、 実態としては「pairing 直後の接続成立」 を反映する
    というよりは「addon 側で session が register された vessel」 を返す。
    実機検証で UX が薄ければ Phase 4' 以降で gateway 経由の死活確認に
    切り替える。
    """
    vm = get_vessel_manager()
    records = vm.list_vessels()
    connected_ids = {s.vessel_id for s in vm.list_sessions()}
    return {
        "vessels": [
            VesselSummary(
                vessel_id=r.vessel_id,
                bound_building_id=r.bound_building_id,
                bound_persona_id=r.bound_persona_id,
                hardware_model=r.hardware_model,
                firmware_version=r.firmware_version,
                paired_at=r.paired_at,
                last_seen_at=r.last_seen_at,
                connected=r.vessel_id in connected_ids,
            ).model_dump()
            for r in records
        ]
    }


@router.delete("/vessels/{vessel_id}")
def delete_vessel(
    vessel_id: str, manager=Depends(get_manager),
) -> dict:
    """ペアリング解除。Building.PHYSICAL_VESSEL_ID を NULL に戻す。

    AddonConfig.master_token / vessel_building_id はクリアしない (= 再ペア
    リング時に上書きされる、 UX で入力欄が空になると不便)。
    """
    from database.models import Building

    vm = get_vessel_manager()
    target = vm.get_vessel(vessel_id)
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Vessel '{vessel_id}' not found",
        )

    db = manager.SessionLocal()
    try:
        building = db.query(Building).filter_by(
            BUILDINGID=target.bound_building_id,
            PHYSICAL_VESSEL_ID=vessel_id,
        ).first()
        if building:
            building.PHYSICAL_VESSEL_ID = None
            db.commit()
    finally:
        db.close()

    deleted = vm.delete_vessel(vessel_id)
    LOGGER.info(
        "delete_vessel: vessel_id=%s building_id=%s deleted=%s",
        vessel_id, target.bound_building_id, deleted,
    )
    return {"deleted": deleted}


# ============================================================================
# Firmware flash endpoints (Phase 2' Step 4)
# ============================================================================
# Stack-chan device の NVS erase / firmware flash を UI から実行する。
# esptool subprocess を backend で起動して、 stdout を SSE で realtime に
# frontend へ stream する。
#
# 2 系統:
#   - POST /flash/erase-nvs → NVS partition のみ消去 (0x9000 / 0x4000、
#     16m.csv 準拠)。 ペアリング解除後の AP モード復帰用、 詰み防止に必須
#   - POST /flash/firmware  → merged-binary.bin を 0x0 に書き込み (初回 /
#     クリーンインストール)
#
# 設計判断:
#   - esptool バイナリは shutil.which で resolve、 見つからなければ uvx
#     fallback (= 環境固有の障壁を最小化)
#   - 進捗は SSE (text/event-stream)。 EventSource API で frontend は
#     simple に購読可
#   - cancel は SSE 切断 = subprocess kill (= asyncio.shield しない)
#   - firmware path は `~/.saiverse/user_data/addon_data/saiverse-stackchan-
#     addon/firmware/merged-binary.bin` を default。 自動 DL は Phase 後半で

import os
import shutil
import subprocess

# NVS partition: stackchan-mcp の partitions/v2/16m.csv 準拠
_NVS_OFFSET = "0x9000"
_NVS_SIZE = "0x4000"


def _resolve_esptool_command() -> list[str]:
    """esptool の起動コマンドを返す。

    優先順位:
      1. ``shutil.which("esptool")`` (= PATH 上の esptool バイナリ、
         例: ``~/.local/bin/esptool.exe`` を ~/.bashrc PATH で公開)
      2. fallback: ``uvx esptool`` (= uv 経由で一時 install + 実行、
         初回は数秒の install 待ち。 stackchan-mcp gateway 自体が uv
         前提なので、 uv が使える前提は確実)

    どちらも見つからなければ HTTPException 503 (= UI 側で「esptool が
    使えない、 uv 入れて」 と案内する経路に乗せる)。
    """
    direct = shutil.which("esptool")
    if direct:
        return [direct]
    uvx = shutil.which("uvx")
    if uvx:
        return [uvx, "esptool"]
    raise HTTPException(
        status_code=503,
        detail=(
            "esptool が見つかりません。 uv (= uvx) を install するか、 "
            "pip install esptool で PATH に通してください。"
        ),
    )


def _firmware_resolve_path() -> Optional[Path]:
    """書き込みに使う merged-binary.bin の path を決定する。

    優先順位:
      1. AddonConfig.firmware_path (UI で path 指定) — 設定済みで存在
         するならそれを返す
      2. ``<SAIVerse repo>/temp/stackchan-mcp/firmware/build/
         merged-binary.bin`` (= ローカル開発者の ESP-IDF build 成果物、
         まはー の手元 fork repo の build dir そのまま参照する。 GPL-3.0
         firmware を addon に複製しないため、 intent doc §8 と整合)
      3. ``~/.saiverse/user_data/addon_data/saiverse-stackchan-addon/
         firmware/merged-binary.bin`` (= 一般ユーザー向け、 GitHub
         Releases から DL して配置する想定の path、 v2 規約に統一)

    どれも見つからなければ None を返す (= 呼び出し側で 404 を返す)。
    """
    from saiverse.addon_config import get_params
    from saiverse.addon_paths import get_addon_data_dir

    # (1) AddonConfig.firmware_path
    params = get_params(_ADDON_NAME_FOR_CONFIG)
    user_path_str = (params.get("firmware_path") or "").strip()
    if user_path_str:
        user_path = Path(user_path_str)
        if user_path.exists():
            return user_path

    # (2) repo 配下のローカル build dir
    # __file__ は ~/.saiverse/... ではなく expansion_data/saiverse-stackchan
    # -addon/api_routes.py を指す (= addon_loader が spec_from_file_location
    # で読むため). そこから親 5 階層上 (api_routes.py → addon → expansion
    # _data → SAIVerse repo root) で repo root を取る。
    repo_root = Path(__file__).resolve().parent.parent.parent
    repo_build = (
        repo_root / "temp" / "stackchan-mcp" / "firmware" / "build"
        / "merged-binary.bin"
    )
    if repo_build.exists():
        return repo_build

    # (3) 一般ユーザー向け配置場所
    user_default = (
        get_addon_data_dir(_ADDON_NAME_FOR_CONFIG)
        / "firmware" / "merged-binary.bin"
    )
    if user_default.exists():
        return user_default

    return None


class FlashPort(BaseModel):
    port: str  # "COM3" 等
    description: str  # "USB Serial Device (COM3)" 等
    vid: Optional[str] = None  # "303A" 等
    pid: Optional[str] = None  # "1001" 等


class FirmwareInfo(BaseModel):
    path: Optional[str] = None
    exists: bool = False
    size: Optional[int] = None
    mtime_iso: Optional[str] = None
    source: str  # "addon_config" / "local_build" / "user_default" / "not_found"


@router.get("/flash/firmware-info", response_model=FirmwareInfo)
def flash_firmware_info() -> FirmwareInfo:
    """書き込みに使われる firmware の情報を返す (UI 表示用)。

    `_firmware_resolve_path()` の解決結果 + どの経路で見つかったかを返す。
    UI 側で「ローカル build dir を使ってる」 「user_default を使ってる」
    「未検出」 等を表示できるようにする。
    """
    from datetime import datetime, timezone
    from saiverse.addon_config import get_params

    fw_path = _firmware_resolve_path()
    if fw_path is None:
        return FirmwareInfo(source="not_found")

    # どの経路で見つかったかを fw_path から逆引きで判定
    params = get_params(_ADDON_NAME_FOR_CONFIG)
    user_path_str = (params.get("firmware_path") or "").strip()
    if user_path_str and str(fw_path) == user_path_str:
        source = "addon_config"
    elif "temp" in fw_path.parts and "stackchan-mcp" in fw_path.parts:
        source = "local_build"
    else:
        source = "user_default"

    try:
        stat = fw_path.stat()
        size = stat.st_size
        mtime_iso = datetime.fromtimestamp(
            stat.st_mtime, tz=timezone.utc,
        ).isoformat()
    except OSError:
        size = None
        mtime_iso = None

    return FirmwareInfo(
        path=str(fw_path),
        exists=True,
        size=size,
        mtime_iso=mtime_iso,
        source=source,
    )


@router.get("/flash/ports", response_model=list[FlashPort])
def list_flash_ports() -> list[FlashPort]:
    """利用可能な COM port を返す。 Windows のみで実装、 ESP32-S3
    (VID 303A) を filter してリストアップする。

    Linux/Mac は将来対応 (= まはー の環境は Windows のみ)。
    """
    if sys.platform != "win32":
        return []

    # PowerShell の Get-PnpDevice で USB Serial Device を列挙、
    # VID 303A / 10C4 (CP210x) / 1A86 (CH340) 等 ESP32 系を filter
    ps_cmd = (
        "Get-PnpDevice -Class Ports -PresentOnly "
        "| Where-Object { $_.Status -eq 'OK' } "
        "| Select-Object Name, DeviceID "
        "| ConvertTo-Json -Compress"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        LOGGER.warning("list_flash_ports: powershell failed: %s", exc)
        return []

    if result.returncode != 0:
        LOGGER.warning(
            "list_flash_ports: powershell rc=%d stderr=%s",
            result.returncode, result.stderr,
        )
        return []

    raw = result.stdout.strip()
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        LOGGER.warning("list_flash_ports: invalid JSON from powershell: %r", raw[:200])
        return []

    # 0 件で null、 1 件で dict、 複数で list が返るので正規化
    if data is None:
        entries: list[dict] = []
    elif isinstance(data, dict):
        entries = [data]
    elif isinstance(data, list):
        entries = data
    else:
        entries = []

    ports: list[FlashPort] = []
    for ent in entries:
        name = ent.get("Name", "") or ""
        device_id = ent.get("DeviceID", "") or ""
        # COM port 抽出: "USB Serial Device (COM3)" → "COM3"
        # name に (COMx) が含まれていれば抽出、 なければ skip
        import re
        m = re.search(r"\((COM\d+)\)", name)
        if not m:
            continue
        port = m.group(1)
        # VID/PID 抽出: "USB\VID_303A&PID_1001\..." 形式
        vid_m = re.search(r"VID_([0-9A-Fa-f]{4})", device_id)
        pid_m = re.search(r"PID_([0-9A-Fa-f]{4})", device_id)
        ports.append(FlashPort(
            port=port,
            description=name,
            vid=vid_m.group(1).upper() if vid_m else None,
            pid=pid_m.group(1).upper() if pid_m else None,
        ))
    return ports


def _stream_esptool(args: list[str], op_label: str):
    """esptool subprocess を起動して stdout を SSE event として yield する。

    SSE 形式: ``data: <json>\\n\\n`` の繰り返し。 各 event の JSON は:
      - {"type": "line", "text": "..."}        # stdout 1 行
      - {"type": "done", "returncode": N}      # 終了
      - {"type": "error", "text": "..."}       # 致命的エラー

    HTTPException は raise しない (= 起動後のエラーは SSE 内で報告、
    起動前のエラー = esptool 不在等は呼び出し側で先に弾く)。
    """
    cmd = _resolve_esptool_command() + args
    LOGGER.info("flash: starting esptool: %s (%s)", cmd, op_label)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,  # line buffered
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, FileNotFoundError) as exc:
        LOGGER.exception("flash: failed to start esptool")
        err_event = {"type": "error", "text": f"esptool 起動失敗: {exc}"}
        yield f"data: {json.dumps(err_event, ensure_ascii=False)}\n\n"
        return

    try:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ""):
            line = line.rstrip("\r\n")
            if not line:
                continue
            event = {"type": "line", "text": line}
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        proc.stdout.close()
        rc = proc.wait(timeout=10)
    except Exception as exc:
        LOGGER.exception("flash: error while streaming esptool output")
        try:
            proc.kill()
        except Exception:
            pass
        err_event = {"type": "error", "text": str(exc)}
        yield f"data: {json.dumps(err_event, ensure_ascii=False)}\n\n"
        return

    done_event = {"type": "done", "returncode": rc}
    yield f"data: {json.dumps(done_event, ensure_ascii=False)}\n\n"
    LOGGER.info("flash: esptool finished rc=%d (%s)", rc, op_label)


def _validate_com_port(port: str) -> str:
    """COM port 文字列を validate + sanitize する。

    subprocess に渡す前に「COM<数字>」 形式しか許可しない (= shell injection
    回避、 ただし Popen で shell=False なので injection は元々ないが安全策)。
    """
    import re
    if not re.fullmatch(r"COM\d+", port):
        raise HTTPException(
            status_code=400, detail=f"Invalid COM port: {port!r}",
        )
    return port


@router.post("/flash/erase-nvs")
def flash_erase_nvs(port: str = Query(...)) -> StreamingResponse:
    """NVS partition のみを erase (= AP モード復帰)。

    ペアリング解除後に device が古い token のままで詰む状況を解消する。
    firmware と OTA は無傷。 数秒で完了。
    """
    port = _validate_com_port(port)
    args = [
        "--chip", "esp32s3", "--port", port,
        "erase-region", _NVS_OFFSET, _NVS_SIZE,
    ]
    return StreamingResponse(
        _stream_esptool(args, f"erase-nvs port={port}"),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx buffering 無効化
        },
    )


@router.post("/flash/firmware")
def flash_firmware(
    port: str = Query(...),
    firmware_path: Optional[str] = Query(None),
) -> StreamingResponse:
    """merged-binary.bin を 0x0 に書き込み (= 初回 / クリーンインストール)。

    NVS も含めて全消去 + 書き込みなので、 既存ペアリング情報はリセット
    される。 完了後 device は AP モードで起動するので、 captive portal
    で新規 Wi-Fi + Token 設定が必要。

    firmware_path 指定なしなら ``_firmware_resolve_path()`` で 3 段階の
    fallback (AddonConfig → ローカル build dir → 一般ユーザー DL 配置)
    を走らせて使う path を決定する。
    """
    port = _validate_com_port(port)
    if firmware_path:
        fw_path: Optional[Path] = Path(firmware_path)
    else:
        fw_path = _firmware_resolve_path()
    if fw_path is None or not fw_path.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                "merged-binary.bin が見つかりません。\n\n"
                "(1) ローカルでビルドするなら "
                "<SAIVerse repo>/temp/stackchan-mcp/firmware/build/ に "
                "merged-binary.bin を生成 (`esptool merge-bin` 等)、 "
                "(2) GitHub Releases から DL する場合は "
                "~/.saiverse/user_data/addon_data/saiverse-stackchan-addon/firmware/ に "
                "配置してください。 もしくは AddonConfig.firmware_path で "
                "絶対 path を指定してください。"
            ),
        )
    # path traversal 簡易チェック (= 任意の path を許す代わりに最低限の
    # validation、 query で渡された場合のみ)
    if not fw_path.is_file():
        raise HTTPException(
            status_code=400, detail=f"Not a file: {fw_path}",
        )

    args = [
        "--chip", "esp32s3", "--port", port,
        "--baud", "921600",
        "--before", "default-reset",
        "--after", "hard-reset",
        "write-flash", "0x0", str(fw_path),
    ]
    return StreamingResponse(
        _stream_esptool(args, f"flash-firmware port={port} fw={fw_path.name}"),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# --- Audio input relay (v0.7: device-driven listen capture) -----------------
#
# stackchan-mcp gateway が device 主導 listen (LCD タッチ / ウェイクワード)
# の Opus 音声を Ogg コンテナにパックして POST してくる経路。詳細は
# audio_input_relay.py の docstring 参照。
from audio_input_relay import audio_router  # noqa: E402

router.include_router(audio_router)


__all__ = ["router"]
