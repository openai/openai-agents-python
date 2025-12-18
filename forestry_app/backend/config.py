import os
from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/forestry"
    )

    # Authentication
    SECRET_KEY: str = os.getenv("SECRET_KEY", "forestry-multiagent-secret-key-2024-secure")
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # OpenAI
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")

    # App
    APP_NAME: str = "Forestry MultiAgent System"
    DEBUG: bool = os.getenv("DEBUG", "false").lower() == "true"

    # Single user credentials
    ADMIN_USERNAME: str = "MelissaBoch"
    ADMIN_PASSWORD: str = "light1way"

    class Config:
        env_file = ".env"
        case_sensitive = True


@lru_cache()
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
