import { type UseQueryResult, useQuery } from "@tanstack/react-query";

import {
  type DataSchema,
  INTROSPECTION_QUERY,
  type IntrospectionResult,
  discoverSchema,
} from "@/lib/dataSchema";
import { useGql } from "@/lib/graphqlClient";

/**
 * Discover the editable table surface via GraphQL introspection.
 *
 * Cached indefinitely for the session — the schema only changes when the
 * backend reintrospects the database (a restart), which would reload the SPA
 * anyway. Shared by the data layout (nav) and each table page.
 */
export function useDataSchema(): UseQueryResult<DataSchema> {
  const gql = useGql();
  return useQuery<DataSchema>({
    queryKey: ["data", "introspection"],
    queryFn: async () => discoverSchema(await gql<IntrospectionResult>(INTROSPECTION_QUERY)),
    staleTime: Number.POSITIVE_INFINITY,
    retry: false,
  });
}
