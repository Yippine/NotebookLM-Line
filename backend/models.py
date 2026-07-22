from datetime import datetime

from pydantic import BaseModel, Field


class InviteVerify(BaseModel):
    code: str = Field(min_length=1, max_length=256)


class AdminLoginRequest(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class SessionTokenOut(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_at: datetime
    channel_id: str | None = None


class ChannelCreate(BaseModel):
    channel_id: str = Field(min_length=1, max_length=255)
    channel_secret: str = Field(min_length=1, max_length=2048)
    channel_access_token: str = Field(min_length=1, max_length=8192)


class ChannelOut(BaseModel):
    channel_id: str
    notebook_id: str | None = None
    notebook_display_name: str | None = None
    binding_status: str = "unbound"
    last_access_checked_at: datetime | None = None
    nlm_bound: bool = False
    webhook_url: str = ""


class NlmLoginRequest(BaseModel):
    storage_state_json: dict


class NotebookSelect(BaseModel):
    notebook_id: str
