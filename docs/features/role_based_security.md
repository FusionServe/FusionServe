# Role-Based Security

## Overview

FusionServe delegates access control entirely to **PostgreSQL's native role system**.  Before every database query — both REST and GraphQL — the session is switched to a configured PostgreSQL role using `SET ROLE`.  This ensures that all permission logic (table privileges, column-level grants, row-level security policies) lives in the database and is enforced uniformly regardless of which API surface is used.

---

## How It Works

Each incoming request triggers [`set_role()`](../../src/fusionserve/persistence.py) at the start of the handler before any SQL is executed:

```python
async def set_role(session: AsyncSession):
    role = settings.anonymous_role
    await session.execute(text(f"SET ROLE '{role}'"))
```

`SET ROLE` switches the effective role of the current PostgreSQL session for the duration of the transaction.  All subsequent queries run with the privileges of that role.  When the session is returned to the connection pool, the role resets to the connection's login user.

---

## Configuration

| Setting | Default | Description |
|---|---|---|
| `anonymous_role` | `fusionserve` | PostgreSQL role applied to every request |

Set via `.env` or environment variable:

```bash
ANONYMOUS_ROLE=api_reader
```

---

## PostgreSQL Role Design

The recommended pattern is to create a dedicated low-privilege role for the API and grant it only the access it needs:

```sql
-- Create a read-only API role
CREATE ROLE api_reader NOLOGIN;
GRANT USAGE ON SCHEMA app_public TO api_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA app_public TO api_reader;

-- Create the login role that FusionServe connects with
CREATE ROLE fusionserve LOGIN PASSWORD '...';
GRANT api_reader TO fusionserve;
```

For write access, add the appropriate DML grants:

```sql
GRANT INSERT, UPDATE, DELETE ON TABLE app_public.users TO api_reader;
```

---

## Row-Level Security

Because every query executes under a PostgreSQL role, **Row-Level Security (RLS)** policies are fully supported.  Define policies on any table and they will be automatically enforced:

```sql
ALTER TABLE app_public.documents ENABLE ROW LEVEL SECURITY;

CREATE POLICY read_own_documents ON app_public.documents
  FOR SELECT
  TO api_reader
  USING (owner_id = current_setting('app.current_user_id')::uuid);
```

---

## Future: JWT / Per-Request Role

The current implementation uses a single static role for all requests.  Per-request role selection (e.g. derived from a JWT claim) is planned:

```python
# TODO: role from jwt or anonymous
role = settings.anonymous_role
```

When JWT support is added, the role will be extracted from the token and passed to `SET ROLE` dynamically, enabling full multi-tenant row-level security without changes to the database schema.

---

## Security Boundary

The database role is the **only** security boundary enforced by FusionServe.  There is no application-layer ACL.  It is strongly recommended to:

- Use a role with the minimum privileges required.
- Enable RLS on sensitive tables.
- Place FusionServe behind a reverse proxy or API gateway if authentication is required before reaching the API.
