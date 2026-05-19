from litestar import get
from litestar.dto import DTOConfig
from litestar.plugins.pydantic import PydanticDTO
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
    default_page_size: int = 50
    max_page_size: int = 1000
    anonymous_role: str = "fusionserve"
    debug: bool = False
    base_path: str = "/api"

    ui_enabled: bool = True
    # ---- UI ----
    #: Public URL where the React SPA is mounted. The OpenAPI router root
    #: at ``base_path`` issues a 302 redirect here (see
    #: :class:`fusionserve.ui.RedirectRenderPlugin`); the SPA itself is
    #: served by the Litestar static-files router built by
    #: :func:`fusionserve.ui.build_spa_route_handler` with
    #: ``html_mode=True`` so ``index.html`` resolves at the mount root
    #: and as a fallback for unmatched paths. Hashed JS/CSS chunks
    #: emitted by Vite use *relative* asset URLs (``./assets/...`` in
    #: ``index.html``), so they're served by the same router from the
    #: ``<ui_path>/assets/`` URL space — no separate asset prefix
    #: setting is needed and the SPA can be relocated by changing only
    #: ``ui_path``.
    #:
    #: The empty-string default is a sentinel: :meth:`_derive_ui_path`
    #: fills it in with ``f"{base_path.rstrip('/')}/-/"`` (default
    #: ``/api/-/``). Setting ``UI_PATH=...`` in the environment skips
    #: that derivation and uses the literal verbatim.
    ui_path: str = ""

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

    @model_validator(mode="after")
    def _derive_ui_path(self):
        """Fill ``ui_path`` from ``base_path`` when the user didn't set it.

        The default ``base_path`` of ``/api`` gives a derived
        ``ui_path`` of ``/api/-/``. Explicit ``UI_PATH=...`` overrides
        in the environment skip this branch (the field arrives
        non-empty from pydantic-settings).
        """
        if not self.ui_path:
            self.ui_path = f"{self.base_path.rstrip('/')}/-/"
        return self


settings = Settings()


# Client configuration endpoint <base_path>/config.json
class ConfigDTO(PydanticDTO[Settings]):
    config = DTOConfig(
        include={
            "jwt_issuer",
            "jwks_url",
            "client_id",
        }
    )


@get(f"{settings.base_path.rstrip('/')}/config.json", dto=ConfigDTO)
async def get_config() -> Settings:
    """Expose a subset of the server configuration to the client.
    This is consumed by the client apps to configure the itself."""
    return settings
