from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    encryption_key: str = ""
    webhook_base_url: str = "https://your-domain.com"
    db_path: str = "data.db"
    admin_password: str = "changeme"
    dealer_contact_info: str = "不過我可以幫您查詢車輛的規格、配備、現有庫存、年份與顏色等資訊，歡迎直接告訴我您想了解的車款！"
    admin_line_user_id: str = ""
    admin_alert_access_token: str = ""
    google_chat_webhook_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    nlm_health_scheduler_enabled: bool = True

    class Config:
        env_file = ".env"


settings = Settings()
