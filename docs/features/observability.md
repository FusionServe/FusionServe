# Observability — Prometheus Metrics

## Overview

FusionServe exposes a **Prometheus-compatible `/metrics` endpoint** out of the box, enabling integration with any monitoring stack that supports the Prometheus scrape model (Prometheus, Grafana, Datadog, etc.).  No additional configuration is required.

---

## Metrics Endpoint

| Path | Description |
|---|---|
| `/metrics` | Prometheus text-format metrics |

```bash
curl http://localhost:8001/metrics
```

Example output:

```
# HELP http_requests_total Total HTTP requests
# TYPE http_requests_total counter
http_requests_total{method="GET",path="/api/users",status="200",metrics="get"} 42.0
...
```

---

## Implementation

Metrics are collected by Litestar's built-in [Prometheus plugin](https://docs.litestar.dev/latest/usage/metrics/prometheus.html).  The plugin is registered in [`main.py`](../../src/fusionserve/main.py) as both a controller and a middleware:

```python
app = Litestar(
    route_handlers=[PrometheusController],           # serves /metrics
    middleware=[
        PrometheusConfig(
            group_path=True,
            labels={"metrics": "get"},
        ).middleware,                                 # instruments all routes
    ],
    ...
)
```

### Configuration Options

| Option | Value | Description |
|---|---|---|
| `group_path` | `True` | Collapses parameterised path segments (e.g. `/api/users/{id}`) into a single metric label, preventing cardinality explosion |
| `labels` | `{"metrics": "get"}` | Static labels added to every metric |

---

## Available Metrics

The Litestar Prometheus middleware automatically collects the following metrics for every HTTP request:

| Metric | Type | Description |
|---|---|---|
| `http_requests_total` | Counter | Total requests, labeled by method, path, and status code |
| `http_request_duration_seconds` | Histogram | Request latency distribution |
| `http_requests_in_progress` | Gauge | Number of requests currently being processed |

---

## Scrape Configuration

Add FusionServe as a scrape target in your `prometheus.yml`:

```yaml
scrape_configs:
  - job_name: fusionserve
    static_configs:
      - targets: ["localhost:8001"]
    metrics_path: /metrics
```

---

## Path Grouping

With `group_path=True`, path parameters are replaced by their template placeholders in metric labels.  This prevents individual UUIDs or IDs from creating unbounded label cardinality:

| Actual path | Metric label |
|---|---|
| `/api/users/abc-123` | `/api/users/{id}` |
| `/api/invoices/xyz-456` | `/api/invoices/{id}` |
