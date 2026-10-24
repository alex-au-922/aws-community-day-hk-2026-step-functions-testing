from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Load the Stripe URL, API key and timeout from ``PAYMENT_`` variables."""

    model_config = SettingsConfigDict(env_prefix="PAYMENT_")

    api_url: AnyHttpUrl
    api_key: SecretStr
    timeout_seconds: float = Field(default=5, gt=0, le=10)
