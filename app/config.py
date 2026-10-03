from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    google_cloud_project: str
    chatbot_api_key: str

    google_cloud_location: str = "us-central1"
    gemini_model: str = "gemini-2.5-flash"
    max_output_tokens: int = 512

    gemini_timeout_seconds: float = 10.0
    hard_timeout_margin_seconds: float = 2.0
    max_retries: int = 2
    backoff_base_seconds: float = 0.5
    max_concurrent_llm_calls: int = 8

    log_level: str = "INFO"
    service_name: str = "llm-chatbot"

    fault_injection_enabled: bool = False


@lru_cache
def get_settings() -> Settings:
    return Settings()
