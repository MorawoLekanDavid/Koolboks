import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";

afterEach(() => { vi.unstubAllGlobals(); document.cookie = "itura_csrf=; Max-Age=0"; });
describe("secure API boundary", () => {
  it("replaces a hosting 404 page with a safe service error", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("The page could not be found", { status: 404 })));
    await expect(api("/api/auth/login", "POST", {})).rejects.toMatchObject({ status: 404, message: "The application service is unavailable. Please try again later or contact your administrator." });
  });
  it("rejects an HTML response even when hosting returns 200", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("<html>Sign in</html>", { status: 200 })));
    await expect(api("/api/auth/me")).rejects.toMatchObject({ status: 502 });
  });
  it("handles successful empty responses", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 204 })));
    await expect(api("/api/auth/logout", "POST")).resolves.toBeUndefined();
  });
  it("sends CSRF in a header and never puts session material in URLs", async () => {
    document.cookie = "itura_csrf=synthetic-csrf";
    const fetch = vi.fn(async (_path: string, _init?: RequestInit) => new Response("{}", { status: 200 })); vi.stubGlobal("fetch", fetch);
    await api("/api/presence", "POST");
    expect(fetch).toHaveBeenCalledWith("/api/presence", expect.objectContaining({ credentials: "same-origin", headers: expect.objectContaining({ "X-CSRF-Token": "synthetic-csrf" }) }));
    expect(fetch.mock.calls[0][0]).not.toContain("key=");
  });
  it("surfaces authentication failures without treating them as success", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"detail":"Sign in required"}', { status: 401 })));
    await expect(api("/api/auth/me")).rejects.toMatchObject({ status: 401, message: "Sign in required" });
  });
  it("preserves AbortSignal cancellation", async () => {
    const controller = new AbortController();
    const fetch = vi.fn(async () => new Response("[]", { status: 200 })); vi.stubGlobal("fetch", fetch);
    await api("/api/conversations", "GET", undefined, controller.signal);
    expect(fetch).toHaveBeenCalledWith("/api/conversations", expect.objectContaining({ signal: controller.signal }));
  });
  it("shows a safe validation message for structured backend errors", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"detail":[{"msg":"bad"}]}', { status: 422 })));
    await expect(api("/api/products", "POST", {})).rejects.toBeInstanceOf(ApiError);
    await expect(api("/api/products", "POST", {})).rejects.toThrow("Please check the supplied values");
  });
});
