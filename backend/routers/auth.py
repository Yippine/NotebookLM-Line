"""Protected NotebookLM setup and centralized course-account routes."""

from __future__ import annotations

import glob
import json
import os
import uuid

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Request

from config import settings
from database import DB
from models import NlmLoginRequest, NotebookSelect
from notebook_models import (
    CourseAccountAuthorizationRequest,
    CourseAccountPublicResponse,
    CourseAccountStatusResponse,
    NotebookBindingRequest,
    NotebookBindingResponse,
)
from services.course_account_service import NotebookServiceError, course_account_service
from services.logging_service import request_id_var
from services.nlm_service import (
    bind_nlm,
    get_binding_status,
    list_notebooks_for_channel,
    recheck_notebook_binding,
    select_notebook,
    test_and_bind_notebook,
    unbind_notebook,
)
from services.notebook_url import NotebookUrlError, parse_notebook_url
from services.rate_limit_service import enforce_rate_limit
from services.session_service import (
    AdminPrincipal,
    SetupPrincipal,
    assert_channel_scope,
    require_admin_session,
    require_setup_session,
)

router = APIRouter(tags=["auth"])


def _request_id() -> str:
    current = request_id_var.get()
    return current if current and current != "-" else uuid.uuid4().hex


def _binding_response(binding, request_id: str) -> NotebookBindingResponse:
    return NotebookBindingResponse(
        **binding.response_dict(),
        request_id=request_id,
    )


def _notebook_http_error(error: NotebookServiceError, request_id: str) -> HTTPException:
    return HTTPException(
        status_code=error.http_status,
        detail={
            "code": error.code,
            "status": error.status,
            "message": error.user_message,
            "request_id": request_id,
        },
    )


def _ensure_shared_binding_enabled() -> None:
    if not settings.shared_notebook_binding_enabled:
        raise HTTPException(404, "此功能尚未啟用")


def _ensure_legacy_admin_tool_enabled() -> None:
    if not settings.legacy_nlm_binding_enabled:
        # 404 avoids advertising a disabled credential-ingestion surface.
        raise HTTPException(404, "遷移工具未啟用")


# ---------------------------------------------------------------------------
# Student shared-binding API
# ---------------------------------------------------------------------------


@router.get("/course-account/public", response_model=CourseAccountPublicResponse)
async def get_public_course_account(
    _principal: SetupPrincipal = Depends(require_setup_session),
):
    _ensure_shared_binding_enabled()
    return await course_account_service.public_status()


@router.post(
    "/channels/{channel_id}/notebook-binding",
    response_model=NotebookBindingResponse,
)
async def bind_shared_notebook(
    channel_id: str,
    body: NotebookBindingRequest,
    request: Request,
    principal: SetupPrincipal = Depends(require_setup_session),
):
    _ensure_shared_binding_enabled()
    assert_channel_scope(principal, channel_id)
    await enforce_rate_limit(
        request, "notebook_binding", subject=f"setup:{principal.session_id}"
    )
    request_id = _request_id()
    try:
        notebook_id = parse_notebook_url(
            body.notebook_url, settings.notebook_host_allowlist_list
        )
    except NotebookUrlError as error:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_notebook_url",
                "status": "error",
                "message": str(error),
                "request_id": request_id,
            },
        ) from error

    try:
        binding = await test_and_bind_notebook(channel_id, notebook_id)
    except KeyError as error:
        raise HTTPException(404, "Channel 不存在") from error
    except NotebookServiceError as error:
        raise _notebook_http_error(error, request_id) from error
    return _binding_response(binding, request_id)


@router.get(
    "/channels/{channel_id}/notebook-binding",
    response_model=NotebookBindingResponse,
)
async def read_shared_notebook_binding(
    channel_id: str,
    principal: SetupPrincipal = Depends(require_setup_session),
):
    _ensure_shared_binding_enabled()
    assert_channel_scope(principal, channel_id)
    request_id = _request_id()
    try:
        binding = await get_binding_status(channel_id)
    except KeyError as error:
        raise HTTPException(404, "Channel 不存在") from error
    return _binding_response(binding, request_id)


@router.post(
    "/channels/{channel_id}/notebook-binding/recheck",
    response_model=NotebookBindingResponse,
)
async def recheck_shared_notebook(
    channel_id: str,
    request: Request,
    principal: SetupPrincipal = Depends(require_setup_session),
):
    _ensure_shared_binding_enabled()
    assert_channel_scope(principal, channel_id)
    await enforce_rate_limit(
        request, "notebook_binding", subject=f"setup:{principal.session_id}"
    )
    request_id = _request_id()
    try:
        binding = await recheck_notebook_binding(channel_id)
    except KeyError as error:
        raise HTTPException(404, "Channel 不存在") from error
    except NotebookServiceError as error:
        raise _notebook_http_error(error, request_id) from error
    return _binding_response(binding, request_id)


@router.delete(
    "/channels/{channel_id}/notebook-binding",
    response_model=NotebookBindingResponse,
)
async def delete_shared_notebook_binding(
    channel_id: str,
    principal: SetupPrincipal = Depends(require_setup_session),
):
    _ensure_shared_binding_enabled()
    assert_channel_scope(principal, channel_id)
    request_id = _request_id()
    try:
        binding = await unbind_notebook(channel_id)
    except KeyError as error:
        raise HTTPException(404, "Channel 不存在") from error
    return _binding_response(binding, request_id)


# Backward-compatible status path; unlike the old endpoint it is scoped.
@router.get("/channels/{channel_id}/nlm-status")
async def nlm_status(
    channel_id: str,
    principal: SetupPrincipal = Depends(require_setup_session),
):
    assert_channel_scope(principal, channel_id)
    try:
        binding = await get_binding_status(channel_id)
    except KeyError as error:
        raise HTTPException(404, "Channel 不存在") from error
    return {
        "bound": binding.status == "bound" and binding.notebook_id is not None,
        **binding.response_dict(),
        "request_id": _request_id(),
    }


# ---------------------------------------------------------------------------
# Administrator course-account API
# ---------------------------------------------------------------------------


@router.get("/admin/course-account", response_model=CourseAccountStatusResponse)
async def get_course_account_status(
    _principal: AdminPrincipal = Depends(require_admin_session),
):
    return {**(await course_account_service.status()), "request_id": _request_id()}


async def _configure_course_account(
    body: CourseAccountAuthorizationRequest,
    request: Request,
    principal: AdminPrincipal,
) -> CourseAccountStatusResponse:
    _ensure_shared_binding_enabled()
    await enforce_rate_limit(
        request, "course_account_reauth", subject=f"admin:{principal.session_id}"
    )
    request_id = _request_id()
    try:
        result = await course_account_service.configure(
            email=body.email,
            auth_payload=body.storage_state_json,
            auth_mode=body.auth_mode,
        )
    except NotebookServiceError as error:
        raise _notebook_http_error(error, request_id) from error
    return CourseAccountStatusResponse(**result, request_id=request_id)


@router.put("/admin/course-account", response_model=CourseAccountStatusResponse)
async def put_course_account(
    body: CourseAccountAuthorizationRequest,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_session),
):
    return await _configure_course_account(body, request, principal)


@router.post(
    "/admin/course-account/reauthenticate",
    response_model=CourseAccountStatusResponse,
)
async def reauthenticate_course_account(
    body: CourseAccountAuthorizationRequest,
    request: Request,
    principal: AdminPrincipal = Depends(require_admin_session),
):
    # configure() probes first and atomically retains the previous auth on
    # failure, so this route is safe to use for both account renewal and swap.
    return await _configure_course_account(body, request, principal)


@router.post(
    "/admin/course-account/health-check",
    response_model=CourseAccountStatusResponse,
)
async def check_course_account_health(
    _principal: AdminPrincipal = Depends(require_admin_session),
):
    _ensure_shared_binding_enabled()
    result = await course_account_service.check_health()
    return {**result, "request_id": _request_id()}


# ---------------------------------------------------------------------------
# Feature-flagged administrator-only legacy migration tools
# ---------------------------------------------------------------------------


def _find_storage_state() -> str | None:
    home = os.path.expanduser("~")
    patterns = [
        os.path.join(home, ".notebooklm", "profiles", "*", "storage_state.json"),
        os.path.join(home, ".notebooklm", "browser_profile", "storage_state.json"),
        os.path.join(
            home, ".config", "notebooklm", "profiles", "*", "storage_state.json"
        ),
    ]
    files: list[str] = []
    for pattern in patterns:
        files.extend(glob.glob(pattern))
    return max(files, key=os.path.getmtime) if files else None


async def _ensure_channel_exists(channel_id: str) -> None:
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(
            "SELECT channel_id FROM channels WHERE channel_id=?", (channel_id,)
        )
        if not await cur.fetchone():
            raise HTTPException(404, "Channel 不存在")


@router.post("/channels/{channel_id}/nlm-login")
async def legacy_nlm_login(
    channel_id: str,
    body: NlmLoginRequest,
    _principal: AdminPrincipal = Depends(require_admin_session),
):
    _ensure_legacy_admin_tool_enabled()
    await _ensure_channel_exists(channel_id)
    request_id = _request_id()
    try:
        notebook_id, notebooks = await bind_nlm(channel_id, body.storage_state_json)
    except NotebookServiceError as error:
        raise _notebook_http_error(error, request_id) from error
    return {
        "status": "bound",
        "notebook_id": notebook_id,
        "notebooks": notebooks,
        "message": "管理員遷移綁定成功",
        "request_id": request_id,
    }


@router.post("/channels/{channel_id}/nlm-bind-local")
async def legacy_nlm_bind_local(
    channel_id: str,
    _principal: AdminPrincipal = Depends(require_admin_session),
):
    _ensure_legacy_admin_tool_enabled()
    await _ensure_channel_exists(channel_id)
    path = _find_storage_state()
    if not path:
        raise HTTPException(400, "伺服器找不到可用的 NotebookLM 授權檔")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            storage_state = json.load(handle)
    except (OSError, ValueError, TypeError) as error:
        raise HTTPException(400, "無法讀取 NotebookLM 授權檔") from error

    request_id = _request_id()
    try:
        notebook_id, notebooks = await bind_nlm(channel_id, storage_state)
    except NotebookServiceError as error:
        raise _notebook_http_error(error, request_id) from error
    return {
        "status": "bound",
        "notebook_id": notebook_id,
        "notebooks": notebooks,
        "message": "管理員遷移綁定成功",
        "request_id": request_id,
    }


@router.get("/channels/{channel_id}/notebooks")
async def legacy_get_notebooks(
    channel_id: str,
    _principal: AdminPrincipal = Depends(require_admin_session),
):
    _ensure_legacy_admin_tool_enabled()
    try:
        notebooks = await list_notebooks_for_channel(channel_id)
    except NotebookServiceError as error:
        raise _notebook_http_error(error, _request_id()) from error
    return {"notebooks": notebooks}


@router.put("/channels/{channel_id}/notebook")
async def legacy_set_notebook(
    channel_id: str,
    body: NotebookSelect,
    _principal: AdminPrincipal = Depends(require_admin_session),
):
    _ensure_legacy_admin_tool_enabled()
    await _ensure_channel_exists(channel_id)
    await select_notebook(channel_id, body.notebook_id)
    return {"status": "ok", "notebook_id": body.notebook_id}
