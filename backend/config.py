from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    encryption_key: str = ""
    webhook_base_url: str = "https://your-domain.com"
    db_path: str = "data.db"
    admin_password: str = "changeme"
    dealer_contact_info: str = "如需購車相關服務，請聯繫業務窗口。"

    class Config:
        env_file = ".env"


settings = Settings()
