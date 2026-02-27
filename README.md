# FusionServe

> Automatic generation of REST and GraphQL APIs by database introspection.

FusionServe introspects a PostgreSQL database schema and automatically generates both REST and GraphQL APIs, making it easy to expose database tables as web APIs without manually writing endpoints.

## Features

- **Automatic API Generation** - Introspects your PostgreSQL schema and creates full CRUD endpoints
- **Dual API Support** - REST API with OpenAPI docs + GraphQL API
- **OData-style Filtering** - Advanced query filtering using OData syntax
- **Pagination** - Built-in pagination with configurable page size
- **Role-based Security** - PostgreSQL role-based access control
- **Prometheus Metrics** - Built-in `/metrics` endpoint
- **GZip Compression** - Optimized response compression

## Quick Start

```bash
# Install
pip install fusionserve

# Run with uvicorn
uvicorn fusionserve.main:app --reload
```

Or using Docker:

```bash
docker run -p 8001:8001 fusionserve
```

## Configuration

FusionServe uses [Dynaconf](https://www.dynaconf.com/) for configuration. Create a `settings.yaml` or `.secrets.yaml` file:

```yaml
default:
  app_name: FusionServe
  pg_host: localhost
  pg_user: postgres
  pg_password: secret
  pg_database: mydb
  pg_app_schema: public
  max_page_lenght: 1000
  anonymous_role: reader
```

### Environment Variables

All settings can be overridden with environment variables (uppercase):

```bash
export PG_HOST=production-db.example.com
export PG_PASSWORD=secret
```

## REST API

Once running, access the Swagger UI at `/api/docs`.

### Endpoints

For each table (e.g., `users`):

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/users` | List all records |
| GET | `/api/users/{pk}` | Get one by primary key |
| POST | `/api/users` | Create new record(s) |
| PATCH | `/api/users/{pk}` | Update record |
| DELETE | `/api/users/{pk}` | Delete record |

### Query Parameters

**Pagination:**
```
GET /api/users?_limit=10&_offset=0
```

**Basic Filtering:**
```
GET /api/users?status=active&role=admin
```

**Advanced OData Filtering:**
```
GET /api/users?_filter=(status eq 'active') and (age gt 18)
```

Supported OData operators: `eq`, `ne`, `gt`, `ge`, `lt`, `le`, `and`, `or`, `not`

## GraphQL API

Access the GraphQL Playground at `/graphql`.

### Query Example

```graphql
query {
  users(limit: 10, offset: 0) {
    nodes {
      id
      name
      email
    }
    totalCount
  }
}
```

## Architecture

```
src/fusionserve/
├── main.py        # FastAPI application entry point
├── config.py      # Dynaconf configuration
├── persistence.py # Database introspection & model generation
├── rest.py        # REST API route generation
├── graphql.py     # GraphQL schema generation
└── models.py      # Pydantic models (pagination, filtering)
```

### How It Works

1. **Startup** - On app startup, FusionServe introspects the PostgreSQL schema
2. **Model Generation** - Creates Pydantic models for each table (CRUD variants)
3. **Route Generation** - Dynamically builds REST and GraphQL endpoints
4. **Request Handling** - Each request executes with the configured PostgreSQL role

## Requirements

- Python 3.8+
- PostgreSQL 12+
- See `setup.cfg` for full dependencies

## License

MIT
