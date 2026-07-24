import { Injectable, inject, signal } from '@angular/core';

import { GraphqlService } from '../../core/graphql.client';
import {
  type DataSchema,
  INTROSPECTION_QUERY,
  type IntrospectionResult,
  discoverSchema,
} from '../../lib/data-schema';

/**
 * Discover the editable table surface via GraphQL introspection.
 *
 * Cached indefinitely for the session — the schema only changes when the
 * backend reintrospects the database (a restart), which would reload the SPA
 * anyway. Shared by the data layout (nav) and each table page: concurrent and
 * repeat callers share one in-flight request.
 */
@Injectable({ providedIn: 'root' })
export class DataSchemaService {
  private readonly gql = inject(GraphqlService);

  /** Resolved schema once loaded, else ``null``. */
  readonly schema = signal<DataSchema | null>(null);
  /** ``true`` while the introspection request is in flight. */
  readonly loading = signal(false);
  /** Human-readable error from a failed introspection, else ``null``. */
  readonly error = signal<string | null>(null);

  private promise: Promise<DataSchema> | null = null;

  /** Load (or return the in-flight/cached) schema. */
  load(): Promise<DataSchema> {
    if (!this.promise) {
      this.loading.set(true);
      this.promise = (async () => {
        try {
          const result = await this.gql.request<IntrospectionResult>(INTROSPECTION_QUERY);
          const schema = discoverSchema(result);
          this.schema.set(schema);
          this.error.set(null);
          return schema;
        } catch (e) {
          this.error.set(e instanceof Error ? e.message : String(e));
          this.promise = null; // allow a retry
          throw e;
        } finally {
          this.loading.set(false);
        }
      })();
    }
    return this.promise;
  }
}
