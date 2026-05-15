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
    # ---- UI / Vite ----
    #: When ``True``, the Litestar Vite plugin starts the Vite dev server
    #: (one-port HMR proxy through Litestar). Leave ``False`` in production —
    #: the SPA is served from prebuilt assets in ``src/fusionserve/web/dist``.
    vite_dev_mode: bool = False

    #: Public URL where the React SPA lives. The OpenAPI router root at
    #: ``base_path`` issues a 302 redirect here (see
    #: :class:`fusionserve.ui.RedirectRenderPlugin`); the SPA itself is
    #: served by ``litestar-vite``'s root catch-all so any unmatched path
    #: resolves to ``index.html``. Surfaced to Vite as ``VITE_BASE_URL``
    #: so the dev-mode proxy and the production HTML transformer resolve
    #: asset paths against the right URL space.
    ui_path: str = "/-/"

    #: URL prefix for hashed JS/CSS chunks emitted by Vite. Must stay
    #: **outside** :attr:`base_path` — the OpenAPI router mounted at
    #: ``base_path`` auto-registers a ``<base_path>/{path:str}`` not-found
    #: handler that would otherwise shadow asset requests. The matching
    #: literal lives in ``ui/vite.config.ts`` (``assetUrl``); the Python
    #: and JS sides must be kept in sync.
    ui_assets_path: str = "/-/assets/"

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
