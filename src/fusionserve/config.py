from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )
    app_name: str = "FusionServe"
    log_level: str = "INFO"
    pg_user: str = "fusionserve"
    pg_password: SecretStr = SecretStr("")
    pg_host: str = "localhost"
    pg_database: str = "fusionserve"
    pg_app_schema: str = "app_public"
    pg_port: int = 5432
    echo_sql: bool = False
    max_page_length: int = 1000
    anonymous_role: str = "fusionserve"
    debug: bool = False
    base_path: str = "/api"

    jwt_issuer: str | None = None
    jwks_url: str | None = None


settings = Settings()
