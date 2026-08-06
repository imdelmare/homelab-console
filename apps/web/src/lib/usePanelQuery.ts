import { createContext, createElement, useContext } from "react";
import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError } from "./api";
import { describeError } from "./ui";
import type { LoadState } from "./ui";

type PanelQueryOptions = {
  // Override the app-wide background refetch cadence; false disables it.
  refetchInterval?: number | false;
  enabled?: boolean;
};

const PanelQueriesEnabledContext = createContext(true);

export function buildPanelQueryOptions(options: PanelQueryOptions, scopeEnabled: boolean) {
  return {
    enabled: scopeEnabled && (options.enabled ?? true),
    ...(options.refetchInterval === undefined ? {} : { refetchInterval: options.refetchInterval }),
  };
}

export function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  return !(error instanceof ApiError && error.status >= 400 && error.status < 500) && failureCount < 1;
}

export function buildPanelQueryFn<T>(queryFn: () => Promise<T>) {
  return () => queryFn();
}

export function PanelQueryScope({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  return createElement(PanelQueriesEnabledContext.Provider, { value: enabled }, children);
}

// Thin wrapper mapping TanStack Query state onto the LoadState/errorMessage
// shape the panels render. Queries with the same key share one cache entry
// and one request across every open window (taskbar included).
export function usePanelQuery<T>(
  queryKey: readonly unknown[],
  queryFn: () => Promise<T>,
  options: PanelQueryOptions = {},
) {
  const scopeEnabled = useContext(PanelQueriesEnabledContext);
  const query = useQuery({
    queryKey,
    // TanStack invokes query functions with a QueryFunctionContext argument.
    // Panel fetchers intentionally have a no-argument contract; wrap them so
    // an optional API parameter cannot accidentally receive that context.
    queryFn: buildPanelQueryFn(queryFn),
    ...buildPanelQueryOptions(options, scopeEnabled),
  });
  const loadState: LoadState = query.isPending ? "loading" : query.isError ? "error" : "ready";
  return {
    data: query.data,
    loadState,
    isFetching: query.isFetching,
    errorMessage: query.isError ? describeError(query.error) : null,
    refresh: () => void query.refetch(),
  };
}

export function combineLoadStates(...states: LoadState[]): LoadState {
  if (states.includes("error")) return "error";
  if (states.includes("loading")) return "loading";
  return "ready";
}
