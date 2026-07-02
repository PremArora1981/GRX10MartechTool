"use client";

import useSWR, { type SWRConfiguration, type SWRResponse } from "swr";
import { apiRequest, type RequestOptions } from "./api";

/**
 * Client-side data hook for screens that need live/refetching data (e.g. the
 * Status page auto-refresh, optimistic connector probes). Server components
 * should call `api.*` directly instead — SWR is only for client interactivity.
 *
 * Usage:
 *   const { data, error, isLoading } = useApi<StatusSnapshot>("/status");
 */
export function useApi<T>(
  path: string | null,
  options?: RequestOptions,
  swrConfig?: SWRConfiguration<T>,
): SWRResponse<T> {
  return useSWR<T>(
    path === null ? null : [path, options],
    ([p, o]: [string, RequestOptions | undefined]) => apiRequest<T>(p, o),
    swrConfig,
  );
}

export { useSWR };
