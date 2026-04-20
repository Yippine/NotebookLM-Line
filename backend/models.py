from pydantic import BaseModel


class InviteVerify(BaseModel):
    code: str


class ChannelCreate(BaseModel):
    channel_id: str
    channel_secret: str
    channel_access_token: str


class ChannelOut(BaseModel):
    channel_id: str
    notebook_id: str | None = None
    nlm_bound: bool = False
    webhook_url: str = ""


class NlmLoginRequest(BaseModel):
    storage_state_json: dict


class NotebookSelect(BaseModel):
    notebook_id: str
