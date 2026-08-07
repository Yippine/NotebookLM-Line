from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    encryption_key: str = ""
    webhook_base_url: str = "https://your-domain.com"
    db_path: str = "data.db"
    admin_password: str = "changeme"
    dealer_contact_info: str = "如需購車相關服務，請聯繫業務窗口。"
    google_oauth_token_path: str = "google-oauth-token.json"
    google_sheet_id: str = ""
    google_drive_parent_folder_id: str = ""
    admin_line_user_id: str = ""
    admin_alert_access_token: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
