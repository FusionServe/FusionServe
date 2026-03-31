# YAML Frontmatter Metadata in Table Descriptions

## Overview

This feature enables embedding structured metadata directly inside a database table's `description` field using **YAML frontmatter** — a well-established convention (popularized by static site generators like Jekyll and Hugo) where a YAML block delimited by `---` markers appears at the top of an otherwise free-text field.

---

## Format Specification

A table description using this feature follows this structure:

```
---
owner: platform-team
domain: billing
tier: critical
pii: true
deprecated: false
tags:
  - invoicing
  - finance
  - tenant-scoped
sla:
  freshness: 15m
  retention: 7y
---
Stores finalized invoice records per tenant. Each row represents a single
billing cycle snapshot. Joined frequently with `payments` and `subscriptions`.
```

The content after the closing `---` is the plain-text human description, preserved as-is. If no frontmatter is present, the field is treated as a legacy plain-text description and remains fully backward compatible.

---

## Parsing Contract

| Rule | Detail |
|---|---|
| Delimiter | Block must open and close with `---` on its own line |
| Position | Frontmatter must appear at the very start of the field (no leading whitespace or newlines) |
| Encoding | Valid YAML 1.2; UTF-8 |
| Fallback | If parsing fails, the entire field is treated as plain text — no error is raised |

---
