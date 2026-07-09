# File Uploads

## Overview

FusionServe exposes a generic file-upload feature over REST built around
**direct-to-store** transfers: object bytes never pass through the
application in the normal flow. The server issues a short-lived
**presigned URL** and the client uploads/downloads straight to/from the
object store (S3 by default; custom backends plug in via a dotted import
path). Per-file metadata lives in a conventional table in your
application schema.

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

Uploads are **two-phase**:

```
1. POST /api/v1/_uploads            ── init ──▶  INSERT pending row
                                                 StorageBackend.generate_upload_url()
   ◀── { items: [ { id, upload_url, method, headers, ... } ] }

2. client ── PUT bytes ──▶ upload_url (object store, directly)

3. POST /api/v1/_uploads/{id}/complete ── HEAD object, enforce size ──▶
                                          UPDATE row SET size/etag, status='completed'
```

Downloads issue a **302 redirect** to a presigned GET URL. The blob keys
are **server-generated** (`YYYY/MM/DD/<uuid4><ext>`); clients never get
to choose the path under which their bytes end up.

### Optional HTTP proxy

When `STORAGE_PROXY_URLS=true`, the `upload_url` returned by *init* and
the redirect target of *download* are **origin-swapped**: the object
store's `scheme://host` is replaced with FusionServe's own base plus a
`_proxy` path prefix, while the path and query (which carry the
`X-Amz-*` signature) are preserved verbatim. The client hits that URL
and FusionServe relays the request/response to the real object store, so
**clients never contact the object store directly** — useful when the
store is on a private network or blocked by client-side egress policy.

```
client ── PUT ──▶ /api/v1/_uploads/_proxy/<key>?X-Amz-...  ── relay ──▶ S3
client ── GET ──▶ /api/v1/_uploads/_proxy/<key>?X-Amz-...  ◀── stream ── S3
```

The relay reconstructs the object-store URL solely from the backend's
own origin (never from client input), so it cannot be pointed at an
arbitrary host. The relay routes are **unauthenticated**: the presigned
signature in the query string is the capability, exactly like a raw
presigned URL. Note the literal origin-swap leaves the `X-Amz-Credential`
query parameter (access-key-id + region) visible to the client; if that
is unacceptable, front the store with a backend that re-signs opaquely.

---

## Required schema

Create the metadata table once, in the schema configured by
`PG_APP_SCHEMA` (default `app_public`):

```sql
CREATE TABLE app_public.uploads (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    filename        text NOT NULL,
    content_type    text NOT NULL,
    size_bytes      bigint CHECK (size_bytes >= 0),   -- NULL until completed
    storage_key     text NOT NULL UNIQUE,
    storage_backend text NOT NULL,
    status          text NOT NULL DEFAULT 'pending',  -- 'pending' | 'completed'
    etag            text,                             -- filled at complete
    attributes      jsonb,                            -- optional client metadata
    uploaded_by     uuid REFERENCES app_public.users(id),
    uploaded_at     timestamptz NOT NULL DEFAULT now()
);
```

`fusionserve.files.metadata.validate_uploads_table` checks this
contract at lifespan startup and fails loudly if columns or types
diverge. `size_bytes`, `etag` and `attributes` may be `NULL`; every
other column must be `NOT NULL` (except `uploaded_by`).

!!! note "Why `attributes`, not `metadata`?"
    The JSONB bag is called `attributes` on purpose: a column literally
    named `metadata` collides with SQLAlchemy's reserved Declarative
    attribute and would break introspection at startup.

!!! warning "Lock the auto-generated CRUD down"
    The same `uploads` table is also exposed by FusionServe's
    introspection-driven REST **and GraphQL** surfaces as a generic
    resource at `/api/v1/uploads` (including the `attributes` column).
    Both allow direct writes to columns like `storage_key`, `size_bytes`
    and `status`, which would let a client mint metadata rows that do not
    match any real blob. **Restrict access via PostgreSQL row-level
    security and column-level `GRANT`s**: typically revoke `INSERT`,
    `UPDATE`, and (if orphan-blob risk is unacceptable) `DELETE` on
    `app_public.uploads` from your application roles, and only allow
    these via the specialized controller running under a privileged role.

---

## Configuration

```bash
# Backend selector. Built-in values: "s3", "azure" (placeholder).
# Any other value is treated as a dotted "pkg.mod:Class" import path.
STORAGE_BACKEND=s3

# Name of the metadata table. The feature gracefully disables when
# absent.
STORAGE_METADATA_TABLE=uploads

# Per-file cap (bytes), enforced at the /complete step.
STORAGE_MAX_SINGLE_FILE_BYTES=104857600  # 100 MiB per file

# Route presigned URLs through FusionServe's HTTP proxy (default off).
STORAGE_PROXY_URLS=false

# --- s3 backend ---
STORAGE_S3__BUCKET=fusionserve-uploads
STORAGE_S3__REGION=eu-central-1
# Optional, for MinIO / LocalStack:
STORAGE_S3__ENDPOINT_URL=
# Optional; otherwise the standard AWS credential chain (env, IAM role) is used:
STORAGE_S3__ACCESS_KEY_ID=
STORAGE_S3__SECRET_ACCESS_KEY=
# TTL applied to both upload and download presigned URLs:
STORAGE_S3__PRESIGN_TTL_SECONDS=3600
```

S3 client requests are issued with
`request_checksum_calculation="when_required"` and
`response_checksum_validation="when_required"` to remain compatible
with S3-compatible backends (MinIO, LocalStack, older R2 versions) that
do not understand the new botocore default of CRC32 trailer checksums.
Deployments using `STORAGE_S3__ENDPOINT_URL` (MinIO/LocalStack) are the
exact/robust case for the proxy, since the origin is taken verbatim from
the endpoint.

---

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST`   | `/api/v1/_uploads`              | Initiate uploads. Body `{files:[{filename, content_type?, attributes?}]}`. Returns 201 with one `items[*]` ticket per file. |
| `POST`   | `/api/v1/_uploads/{id}/complete`| Finalize: verify the object, record size/etag, mark `completed`. Optional body `{attributes}` overwrites the JSONB bag. |
| `GET`    | `/api/v1/_uploads/{id}/content` | 302 redirect to a presigned GET URL (or its proxied form). |
| `DELETE` | `/api/v1/_uploads/{id}`         | Cascading delete: blob first, then metadata row. |
| `PUT`/`GET` | `/api/v1/_uploads/_proxy/{path}` | Internal relay used only when `STORAGE_PROXY_URLS=true`; not called directly. |

### Upload (two-phase)

```bash
# 1. Initiate — get a presigned URL + a pending row id.
curl -X POST http://localhost:8001/api/v1/_uploads \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"files":[{"filename":"report.pdf","content_type":"application/pdf","attributes":{"project":"acme"}}]}'
```

Response (`HTTP 201`):

```json
{
  "items": [
    {
      "id": "8c5b6e54-...",
      "filename": "report.pdf",
      "storage_key": "2026/05/19/8c5b6e54....pdf",
      "upload_url": "https://bucket.s3.eu-central-1.amazonaws.com/2026/05/19/8c5b6e54....pdf?X-Amz-...",
      "method": "PUT",
      "headers": { "Content-Type": "application/pdf" },
      "expires_at": "2026-05-19T14:42:09Z"
    }
  ]
}
```

```bash
# 2. Upload the bytes directly to the object store (send the headers).
curl -X PUT "$UPLOAD_URL" -H "Content-Type: application/pdf" --data-binary @./report.pdf

# 3. Finalize — the server verifies and records the real size/etag.
curl -X POST http://localhost:8001/api/v1/_uploads/8c5b6e54-.../complete \
  -H "Authorization: Bearer $TOKEN"
```

`complete` returns the finalized metadata row. It responds `409` if the
object is not present in the store yet, and `413` (deleting the blob and
row) if the uploaded object exceeds `STORAGE_MAX_SINGLE_FILE_BYTES`.

### Download

```bash
curl -L http://localhost:8001/api/v1/_uploads/8c5b6e54-.../content \
     -H "Authorization: Bearer $TOKEN" -o report.pdf
```

The endpoint 302-redirects to a presigned GET URL. With
`STORAGE_PROXY_URLS=true` the redirect points at the `_proxy` relay and
the bytes stream back through FusionServe.

### Delete

```bash
curl -X DELETE http://localhost:8001/api/v1/_uploads/8c5b6e54-... \
     -H "Authorization: Bearer $TOKEN"
```

Returns `204 No Content`. The blob is removed from the backend **before**
the SQL row is deleted; a backend error aborts the entire operation
(no orphan rows). The auto-generated `DELETE /api/v1/uploads/{id}` will
also delete the metadata row but does **not** touch the backend — use
the controller endpoint above unless you have a deliberate reason to
orphan the blob.

---

## Custom backends

Subclass nothing — implement the
`fusionserve.storage.StorageBackend` `Protocol`:

```python
# mypkg/storage.py
import datetime

from fusionserve.storage.base import StorageObject, UploadTicket


class MyBackend:
    async def generate_upload_url(self, key, *, content_type, expires_in) -> UploadTicket:
        ...
        return UploadTicket(url=url, method="PUT", headers={"Content-Type": content_type},
                            expires_at=datetime.datetime.now(datetime.UTC))

    async def generate_download_url(self, key, *, expires_in) -> str: ...
    async def stat(self, key) -> StorageObject: ...
    async def delete(self, key) -> None: ...
    async def object_origin(self) -> str: ...  # scheme://host presigned URLs use
```

Point FusionServe at it via the dotted import-path syntax:

```bash
STORAGE_BACKEND=mypkg.storage:MyBackend
```

`fusionserve.storage.load_backend` resolves the import, instantiates
the class with no arguments (read your own settings from
`fusionserve.config`), and verifies the result satisfies the protocol.

The bundled `azure` selector resolves to
`fusionserve.storage.azure.AzureBlobBackend`, a **placeholder** whose
methods raise `NotImplementedError`; it reserves the name for a future
SAS-based implementation.

---

## What's not in v1

- **Resumable uploads** (tus, S3 multipart resume): intentionally
  omitted as fragile and non-standard.
- **GraphQL upload mutation**: the dynamic schema builder is
  deliberately not touched.
- **Per-upload destination override** (client-chosen bucket/folder):
  destination is fully config-driven.
- **Image processing / virus scanning hooks**: out of scope.
- **Server-side buffered uploads**: bytes only ever pass through the
  app via the optional relay proxy; there is no multipart-to-disk path.
