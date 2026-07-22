"""Pydantic models for centralized NotebookLM account and bindings."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


CourseHealthStatus = Literal["unconfigured", "healthy", "expired", "error"]
BindingStatus = Literal[
    "unbound",
    "checking",
    "bound",
    "access_revoked",
    "course_account_unavailable",
    "error",
]


class NotebookBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notebook_url: str = Field(min_length=1, max_length=2048)


class NotebookBindingResponse(BaseModel):
    status: BindingStatus
    notebook_id: str | None = None
    notebook_title: str | None = None
    last_access_checked_at: datetime | None = None
    request_id: str


class CourseAccountAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    storage_state_json: dict[str, Any]
    auth_mode: Literal["storage_state"] = "storage_state"

    @field_validator("email")
    @classmethod
    def validate_email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if (
            normalized.count("@") != 1
            or normalized.startswith("@")
            or normalized.endswith("@")
        ):
            raise ValueError("email format is invalid")
        return normalized


class CourseAccountStatusResponse(BaseModel):
    email: str | None = None
    health_status: CourseHealthStatus
    auth_mode: str | None = None
    last_success_at: datetime | None = None
    last_checked_at: datetime | None = None
    error_code: str | None = None
    request_id: str | None = None


class CourseAccountPublicResponse(BaseModel):
    email: str | None = None
    health_status: CourseHealthStatus
