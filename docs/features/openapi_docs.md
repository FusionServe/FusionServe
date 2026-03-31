# OpenAPI Documentation

## Overview

FusionServe automatically generates a fully typed **OpenAPI 3.x specification** from the introspected schema and exposes it through two interactive documentation UIs: **Swagger UI** and **Scalar**.  Both UIs are available at startup with no configuration required.

---

## Endpoints

| Path | Description |
|---|---|
| `/api/docs` | Interactive API documentation (Swagger UI — default) |
| `/api/docs/scalar` | Scalar API documentation UI |
| `/api/openapi.json` | Raw OpenAPI JSON specification |

---

## Swagger UI

[Swagger UI](https://swagger.io/tools/swagger-ui/) is the classic OpenAPI browser.  FusionServe enables several display enhancements:

| Option | Value | Effect |
|---|---|---|
| `displayRequestDuration` | `true` | Shows the elapsed time for each request made from the UI |
| `filter` | `true` | Adds a search box to filter endpoints by path or tag |
| `showExtensions` | `true` | Displays OpenAPI vendor extensions (e.g. custom metadata) |

---

## Scalar

[Scalar](https://scalar.com/) is a modern, visually polished alternative to Swagger UI.  FusionServe configures it with a dark-mode theme:

```python
ScalarRenderPlugin(
    options={
        "theme": "elysiajs",
        "defaultOpenFirstTag": False,
        "darkMode": True,
    }
)
```

| Option | Value | Effect |
|---|---|---|
| `theme` | `elysiajs` | Clean, minimal colour scheme |
| `defaultOpenFirstTag` | `false` | All tag groups start collapsed |
| `darkMode` | `true` | Dark background by default |

---

## OpenAPI Specification Contents

The generated specification includes:

- **Paths** — one entry per CRUD operation per table (5 operations × N tables).
- **Schemas** — Pydantic model definitions for every request and response body, with column names, types, and optionality derived from the database schema.
- **Field descriptions** — column comments are forwarded as `description` fields on individual properties.
- **Tags** — each table's endpoints are grouped under a tag containing the table name and its human-readable description (extracted from the table comment's plain-text portion).
- **Summaries & descriptions** — auto-generated from the table name and the singularised noun (e.g. "Get a user", "List users").
- **Error responses** — `404 Not Found` and `400 Bad Request` are documented for relevant operations.

---

## Implementation

The OpenAPI configuration is set up in [`main.py`](../../src/fusionserve/main.py):

```python
openapi_config=OpenAPIConfig(
    title=settings.app_name,
    version="1.0.0",
    path="/api/docs",
    root_schema_site="swagger",
    render_plugins=[
        SwaggerRenderPlugin(),
        ScalarRenderPlugin(...),
    ],
)
```

`root_schema_site="swagger"` makes Swagger UI the default when navigating to `/api/docs`; Scalar is available at the `/api/docs/scalar` sub-path.

---

## Programmatic Access

The raw OpenAPI JSON can be consumed by any compatible tooling (code generators, testing frameworks, API gateways):

```bash
curl http://localhost:8001/api/openapi.json | jq .
```

The specification is regenerated on each application restart to reflect any schema changes in the database.
