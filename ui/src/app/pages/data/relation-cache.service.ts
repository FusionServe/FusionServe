import { Injectable } from '@angular/core';

/**
 * Session-scoped cache for lazily-fetched row relation details.
 *
 * Keyed by ``<table>:<rowKey>``, it lets a collapsed-then-re-expanded row
 * render its related records instantly instead of re-issuing the PK-lookup.
 * The cache is cleared by {@link DataTablePage} whenever its rows reload
 * (which happens after every create/update/delete and on table switch), so it
 * never serves stale relations across a mutation.
 */
@Injectable({ providedIn: 'root' })
export class RelationCacheService {
  private readonly cache = new Map<string, Record<string, unknown>>();

  get(key: string): Record<string, unknown> | undefined {
    return this.cache.get(key);
  }

  set(key: string, value: Record<string, unknown>): void {
    this.cache.set(key, value);
  }

  /** Drop cached details for a single table (all its rows), or everything. */
  clear(tablePrefix?: string): void {
    if (!tablePrefix) {
      this.cache.clear();
      return;
    }
    const prefix = `${tablePrefix}:`;
    for (const key of this.cache.keys()) {
      if (key.startsWith(prefix)) this.cache.delete(key);
    }
  }
}
