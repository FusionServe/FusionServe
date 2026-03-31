# Response Compression

## Overview

FusionServe automatically compresses HTTP responses using **Brotli**, with a transparent fallback to **GZip** for clients that do not support Brotli.  Compression is applied globally to all responses via Litestar's built-in compression middleware — no per-endpoint configuration is required.

---

## How It Works

Compression is configured in [`main.py`](../../src/fusionserve/main.py) via `CompressionConfig`:

```python
compression_config=CompressionConfig(
    backend="brotli",
    brotli_gzip_fallback=True,
)
```

Litestar inspects the `Accept-Encoding` request header and applies the appropriate codec:

| Client `Accept-Encoding` | Applied compression |
|---|---|
| `br` | Brotli |
| `gzip` (no `br`) | GZip (fallback) |
| neither | No compression |

---

## Brotli

[Brotli](https://github.com/google/brotli) is a modern compression algorithm developed by Google.  Compared to GZip, Brotli typically achieves:

- **20–26% smaller** payloads for text-based content (JSON, HTML).
- Slightly higher CPU cost at compression time, which is offset by the reduced network transfer.

Brotli is supported by all major modern browsers and HTTP clients.

---

## GZip Fallback

When `brotli_gzip_fallback=True` and the client does not advertise `br` in its `Accept-Encoding`, Litestar transparently falls back to GZip.  This ensures backward compatibility with older clients and tools such as `curl` (without explicit `--compressed` Brotli support).

---

## Impact on API Responses

Compression is particularly beneficial for:

- **List endpoints** returning many records — large JSON arrays compress very well.
- **GraphQL responses** with deeply nested data.

Clients can opt out of compression by omitting `Accept-Encoding` from the request, in which case the response is returned as plain JSON.
