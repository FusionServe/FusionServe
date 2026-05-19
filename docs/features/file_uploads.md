# File Uploads

## Overview

FusionServe exposes a generic file-upload feature over REST. Blobs are
persisted by a pluggable **storage backend** (local filesystem or S3 by
default; custom backends plug in via a dotted import path) and the
per-file metadata lives in a conventional table in your application
schema. Multi-file uploads are supported in a single request.

The feature **opts in** automatically when an operator-supplied
`uploads` table is present in `pg_app_schema`; otherwise the relevant
routes are not registered and the rest of FusionServe continues to
start.

---

## Architecture

The specialized **files controller** mounts at
`<base_path>/v1/_uploads` (default `/api/v1/_uploads`). The leading
underscore namespaces these routes away from the auto-generated REST
CRUD that introspection produces at `/api/v1/uploads` for the metadata
table — the two coexist.

```
client ── multipart/form-data ──▶ POST /api/v1/_uploads
                                    │
                                    ├─▶ StorageBackend.save(...)   ──▶ filesystem / S3 / custom
                                    └─▶ INSERT INTO app_public.uploads
```

The blob keys are **server-generated** (`YYYY/MM/DD/<uuid4><ext>`);
clients never get to choose the path under which their bytes end up.

---

## Required schema

Create the metadata table once, in the schema configured by
`PG_APP_SCHEMA` (default `app_public`):

```sql
CREATE TABLE app_public.uploads (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    filename        text NOT NULL,
    content_type    text NOT NULL,
    size_bytes      bigint NOT NULL CHECK (size_bytes >= 0),
    storage_key     text NOT NULL UNIQUE,
    storage_backend text NOT NULL,
    uploaded_by     uuid REFERENCES app_public.users(id),
    uploaded_at     timestamptz NOT NULL DEFAULT now()
);
```

`fusionserve.files.metadata.validate_uploads_table` checks this
contract at lifespan startup and fails loudly if columns or types
diverge.

!!! warning "Lock the auto-generated CRUD down"
    The same `uploads` table is also exposed by FusionServe's
    introspection-driven REST **and GraphQL** surfaces as a generic
    resource at `/api/v1/uploads` and through the GraphQL CRUD
    mutations. Both allow direct writes to columns like `storage_key`
    and `size_bytes`, which would let a client mint metadata rows that
    do not match any real blob. **Restrict access via PostgreSQL row-
    level security and column-level `GRANT`s**: typically revoke
    `INSERT`, `UPDATE`, and (if orphan-blob risk is unacceptable)
    `DELETE` on `app_public.uploads` from your application roles, and
    only allow these via the specialized controller running under a
    privileged role.

---

## Configuration

```bash
# Backend selector. Built-in values: "filesystem", "s3".
# Any other value is treated as a dotted "pkg.mod:Class" import path.
STORAGE_BACKEND=filesystem

# Name of the metadata table. The feature gracefully disables when
# absent.
STORAGE_METADATA_TABLE=uploads

# Size limits.
STORAGE_MAX_TOTAL_BYTES=524288000        # 500 MiB aggregate per request
STORAGE_MAX_SINGLE_FILE_BYTES=104857600  # 100 MiB per file

# --- filesystem backend ---
STORAGE_FS_ROOT=/var/lib/fusionserve/uploads

# --- s3 backend ---
STORAGE_S3__BUCKET=fusionserve-uploads
STORAGE_S3__REGION=eu-central-1
# Optional, for MinIO / LocalStack:
STORAGE_S3__ENDPOINT_URL=
# Optional; otherwise the standard AWS credential chain (env, IAM role) is used:
STORAGE_S3__ACCESS_KEY_ID=
STORAGE_S3__SECRET_ACCESS_KEY=
STORAGE_S3__PRESIGN_TTL_SECONDS=3600
```

S3 client requests are issued with
`request_checksum_calculation="when_required"` and
`response_checksum_validation="when_required"` to remain compatible
with S3-compatible backends (MinIO, LocalStack, older R2 versions) that
do not understand the new botocore default of CRC32 trailer checksums.

---

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST`   | `/api/v1/_uploads`              | Multipart upload, one or more `file` parts. Returns 207 with a per-file status array. |
| `GET`    | `/api/v1/_uploads/{id}/content` | Stream the blob. `?redirect=1` returns 302 to a presigned URL when the backend supports it. |
| `DELETE` | `/api/v1/_uploads/{id}`         | Cascading delete: blob first, then metadata row. |

### Upload

```bash
curl -X POST http://localhost:8001/api/v1/_uploads \
  -H "Authorization: Bearer $TOKEN" \
  -F "data=@./report.pdf" \
  -F "data=@./logo.png"
```

Response (`HTTP 207 Multi-Status`):

```json
{
  "items": [
    {
      "status": "ok",
      "filename": "report.pdf",
      "upload": {
        "id": "8c5b6e54-...",
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "size_bytes": 124356,
        "storage_key": "2026/05/19/8c5b6e54...pdf",
        "storage_backend": "FilesystemBackend",
        "uploaded_by": "f81d0e0c-...",
        "uploaded_at": "2026-05-19T13:42:09Z"
      }
    },
    { "status": "ok", "filename": "logo.png", "upload": { "..." : "..." } }
  ]
}
```

**Partial failures** (e.g. one file exceeds
`STORAGE_MAX_SINGLE_FILE_BYTES`) produce an `error` entry in the same
response — already-succeeded files are not rolled back. Clients are
expected to retry only the failed parts.

### Download

```bash
# Stream the bytes through FusionServe.
curl http://localhost:8001/api/v1/_uploads/8c5b6e54-.../content \
     -H "Authorization: Bearer $TOKEN" -o report.pdf

# Bypass the server bandwidth (S3 only): get a presigned URL via 302.
curl -L http://localhost:8001/api/v1/_uploads/8c5b6e54-.../content?redirect=1 \
     -H "Authorization: Bearer $TOKEN" -o report.pdf
```

The filesystem backend ignores `?redirect=1` and falls back to
streaming.

### Delete

```bash
curl -X DELETE http://localhost:8001/api/v1/_uploads/8c5b6e54-... \
     -H "Authorization: Bearer $TOKEN"
```

Returns `204 No Content`. The blob is removed from the backend **before**
the SQL row is deleted; a backend error aborts the entire operation
(no orphan rows).

The auto-generated `DELETE /api/v1/uploads/{id}` will also delete the
metadata row but does **not** touch the backend — use the controller
endpoint above unless you have a deliberate reason to orphan the blob.

---

## Custom backends

Subclass nothing — implement the
`fusionserve.storage.StorageBackend` `Protocol`:

```python
# mypkg/storage.py
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fusionserve.storage.base import StorageObject


class MyBackend:
    async def save(self, key, stream, *, content_type, declared_size):
        ...
        return StorageObject(key=key, size_bytes=size, content_type=content_type)

    @asynccontextmanager
    async def open(self, key):
        async def _iter():
            yield ...
        yield _iter()

    async def delete(self, key): ...
    async def stat(self, key): ...
    async def presigned_url(self, key, *, expires_in): return None
```

Point FusionServe at it via the dotted import-path syntax:

```bash
STORAGE_BACKEND=mypkg.storage:MyBackend
```

`fusionserve.storage.load_backend` resolves the import, instantiates
the class with no arguments (read your own settings from
`fusionserve.config`), and verifies the result satisfies the protocol.

---

## What's not in v1

- **Resumable uploads** (tus, S3 multipart resume): intentionally
  omitted as fragile and non-standard.
- **GraphQL upload mutation**: the dynamic schema builder is
  deliberately not touched.
- **Per-upload destination override** (client-chosen bucket/folder):
  destination is fully config-driven.
- **Image processing / virus scanning hooks**: out of scope.
