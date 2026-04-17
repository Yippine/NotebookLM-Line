from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    encryption_key: str = ""
    webhook_base_url: str = "https://your-domain.com"
    db_path: str = "data.db"

    class Config:
        env_file = ".env"


settings = Settings()
