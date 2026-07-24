from litestar import get
from litestar.dto import DTOConfig
from litestar.plugins.pydantic import PydanticDTO
from pydantic import BaseModel, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class S3Settings(BaseModel):
    """Configuration for the S3 storage backend.

    Mapped to ``STORAGE_S3__<FIELD>`` environment variables via the
    nested-delimiter mechanism in :class:`Settings`.

    Attributes:
        bucket: Target S3 bucket name. Required when ``storage_backend="s3"``.
        region: AWS region of the bucket.
        endpoint_url: Optional custom endpoint URL for S3-compatible backends
            (MinIO, LocalStack, etc.). When ``None`` the default AWS endpoint
            for the configured region is used.
        access_key_id: AWS access key. When ``None`` aioboto3 falls back to
            the standard credential resolution chain (env vars, IAM role).
        secret_access_key: AWS secret key. See ``access_key_id``.
        presign_ttl_seconds: Lifetime (seconds) of the presigned upload
            and download URLs issued by
            :class:`fusionserve.storage.s3.S3Backend`.
    """

    bucket: str = ""
    region: str | None = None
    endpoint_url: str | None = None
    access_key_id: str | None = None
    secret_access_key: SecretStr | None = None
    presign_ttl_seconds: int = 3600


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
    #: whether to enable the Litestar debug messages in responses.
    debug: bool = False
    #: Whether to log SQL statements executed by SQLAlchemy.
    echo_sql: bool = False
    #: Whether to print the SDL of the GraphQL schema at startup.
    echo_sdl: bool = False
    pg_user: str = "fusionserve"
    pg_password: SecretStr = SecretStr("")
    pg_host: str = "localhost"
    pg_database: str = "fusionserve"
    pg_app_schema: str = "app_public"
    pg_port: int = 5432
    default_page_size: int = 50
    max_page_size: int = 1000
    anonymous_role: str = "fusionserve"

    base_path: str = "/api"

    ui_enabled: bool = True
    # ---- UI ----
    #: Public URL where the Angular SPA is mounted. The OpenAPI router root
    #: at ``base_path`` issues a 302 redirect here (see
    #: :class:`fusionserve.ui.RedirectRenderPlugin`); the SPA itself is
    #: served by the two handlers built by
    #: :func:`fusionserve.ui.build_spa_route_handler` — an assets
    #: static-files router at ``<ui_path>assets`` plus a base-href-injecting
    #: ``index.html`` handler that also serves the deep-link fallback. Angular
    #: emits its whole browser bundle under ``assets/`` with *relative* asset
    #: URLs, so the chunks are served from the ``<ui_path>assets/`` URL space
    #: — no separate asset prefix setting is needed and the SPA can be
    #: relocated by changing only ``ui_path``.
    #:
    #: The empty-string default is a sentinel: :meth:`_derive_ui_path`
    #: fills it in with ``f"{base_path.rstrip('/')}/-/"`` (default
    #: ``/api/-/``). Setting ``UI_PATH=...`` in the environment skips
    #: that derivation and uses the literal verbatim.
    ui_path: str = ""

    # ---- Storage / file uploads ----
    #: Backend selector. ``"s3"`` resolves to the bundled
    #: :class:`fusionserve.storage.s3.S3Backend`; ``"azure"`` to the
    #: :class:`fusionserve.storage.azure.AzureBlobBackend` placeholder.
    #: Any other value is treated as a dotted import path
    #: ``"pkg.mod:ClassName"`` and loaded via
    #: :func:`fusionserve.storage.load_backend`.
    storage_backend: str = "s3"
    #: Name of the metadata table (in ``pg_app_schema``) the files
    #: controller consults. When absent, the files feature is silently
    #: disabled at startup.
    storage_metadata_table: str = "uploads"
    #: Per-file cap (in bytes). Enforced at the ``complete`` step by
    #: HEAD-ing the uploaded object; oversize objects are deleted and
    #: rejected. Also bounds the proxy relay when proxying is enabled.
    storage_max_single_file_bytes: int = 100 * 1024 * 1024
    #: When true, presigned upload/download URLs handed to clients are
    #: rewritten to point at FusionServe's own HTTP proxy (the ``proxy``
    #: relay in :mod:`fusionserve.files.controller`) instead of the object
    #: store, so clients never talk to the object store directly. Off by
    #: default.
    storage_proxy_urls: bool = False
    #: Nested S3 settings (``STORAGE_S3__BUCKET=…`` etc.).
    storage_s3: S3Settings = S3Settings()

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
            "base_path",
        }
    )


@get(
    ["/.well-known/config.json", "/.well-known/configuration", f"{settings.base_path.rstrip('/')}/config.json"],
    dto=ConfigDTO,
)
async def get_config() -> Settings:
    """Expose a subset of the server configuration to the client.
    This is consumed by the web app to configure itself."""
    return settings
