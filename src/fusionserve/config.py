from pydantic import BaseModel, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ClaimsMap(BaseModel):
    username: str
    id: str
    email: str
    display_name: str
    first_name: str
    surname: str
    roles: str
    role: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
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
    client_id: str | None = app_name.lower()
    claims_map: ClaimsMap = ClaimsMap(
        username="/preferred_username",
        id="/sub",
        email="/email",
        display_name="/name",
        first_name="/given_name",
        surname="/family_name",
        roles="",
        role="",
    )

    @model_validator(mode="after")
    def _fill_claims_map(self):
        self.claims_map.roles = f"/resource_access/{self.client_id}/roles"
        self.claims_map.role = f"/resource_access/{self.client_id}/roles/0"
        return self


settings = Settings()
