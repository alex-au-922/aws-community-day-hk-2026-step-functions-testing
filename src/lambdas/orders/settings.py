from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read Orders and stock table names from ORDERS_ environment variables."""

    model_config = SettingsConfigDict(env_prefix="ORDERS_")

    table_name: str = Field(min_length=1)
    stock_table_name: str = Field(min_length=1)
