import { describe, expect, it, vi } from "vitest";
import { ApiError } from "./api";
import {
  buildPanelQueryFn,
  buildPanelQueryOptions,
  shouldRetryQuery,
} from "./usePanelQuery";

describe("buildPanelQueryFn", () => {
  it("does not forward TanStack Query context into no-argument fetchers", async () => {
    const fetcher = vi.fn(async () => ["ok"]);
    const queryFn = buildPanelQueryFn(fetcher) as (...args: unknown[]) => Promise<string[]>;

    await expect(queryFn({ queryKey: ["observations"] })).resolves.toEqual(["ok"]);
    expect(fetcher).toHaveBeenCalledWith();
  });
});

describe("buildPanelQueryOptions", () => {
  it("does not mask QueryClient defaults with undefined overrides", () => {
    expect(buildPanelQueryOptions({}, true)).toEqual({ enabled: true });
  });

  it("combines the auth scope with local enablement and polling overrides", () => {
    expect(buildPanelQueryOptions({ enabled: true, refetchInterval: 60_000 }, false)).toEqual({
      enabled: false,
      refetchInterval: 60_000,
    });
    expect(buildPanelQueryOptions({ refetchInterval: false }, true)).toEqual({
      enabled: true,
      refetchInterval: false,
    });
  });
});

describe("shouldRetryQuery", () => {
  it("does not retry authentication or other client errors", () => {
    expect(shouldRetryQuery(0, new ApiError(401, "Session expired"))).toBe(false);
    expect(shouldRetryQuery(0, new ApiError(404, "Missing"))).toBe(false);
  });

  it("retries a network/server failure only once", () => {
    expect(shouldRetryQuery(0, new ApiError(503, "Unavailable"))).toBe(true);
    expect(shouldRetryQuery(1, new ApiError(503, "Unavailable"))).toBe(false);
    expect(shouldRetryQuery(0, new TypeError("Failed to fetch"))).toBe(true);
  });
});
