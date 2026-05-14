// Static URL constants for the FusionServe backend endpoints the SPA
// embeds via iframes. All URLs live under ``settings.base_path``
// (``/api``) on the Python side and are auto-registered by Litestar's
// OpenAPI router (Swagger / Scalar render plugins) or the dynamic
// GraphQL controller built during the app lifespan.

/** Public URL of the raw OpenAPI 3.1 specification document. */
export const OPENAPI_URL = "/api/openapi.json";

/** Public URL of the backend-served Swagger UI viewer. */
export const SWAGGER_URL = "/api/swagger";

/** Public URL of the GraphQL endpoint (also serves GraphiQL on GET). */
export const GRAPHQL_URL = "/api/graphql";
