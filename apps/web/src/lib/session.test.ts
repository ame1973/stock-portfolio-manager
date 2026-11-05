import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";
import { clearSession } from "./session";
import { ApiRequestError, checked } from "./api";

describe("private session state", () => {
  it("cancels in-flight owner/private reads before dropping private data", async () => {
    const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    cache.setQueryData(["setup"], { initialized: true });
    cache.setQueryData(["owner"], { id: "synthetic-owner" });
    cache.setQueryData(["accounts"], { private: "synthetic-only" });
    let finish: (value: { id: string }) => void = () => { throw new Error("Request not started"); };
    let signal: AbortSignal | undefined;
    const request = cache.fetchQuery({ queryKey: ["owner"], queryFn: ({ signal: abort }) => {
      signal = abort;
      return new Promise<{ id: string }>(resolve => { finish = resolve; });
    } }).catch(() => undefined);
    await clearSession(cache);
    expect(signal?.aborted).toBe(true);
    finish({ id: "late-synthetic-owner" });
    await request;
    expect(cache.getQueryData(["owner"])).toBeNull();
    expect(cache.getQueryData(["accounts"])).toBeUndefined();
    expect(cache.getQueryData(["setup"])).toEqual({ initialized: true });
    expect(cache.getMutationCache().getAll()).toHaveLength(0);
    cache.clear();
  });

  it("rejects missing data and preserves the server error code", () => {
    expect(() => checked({}, false)).toThrow(ApiRequestError);
    try { checked({ error: { code: "PRECONDITION_FAILED" } }, false); }
    catch (cause) {
      expect(cause).toBeInstanceOf(ApiRequestError);
      expect((cause as ApiRequestError).code).toBe("PRECONDITION_FAILED");
      expect((cause as Error).message).toContain("不會覆蓋新版");
    }
  });
});
