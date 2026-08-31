import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

const BACKEND = "https://backend.example";
const OWNER_TOKEN = "owner-token-for-test";
const READ_TOKEN = "read-token-for-test";

async function loadRoute() {
  vi.resetModules();
  vi.stubEnv("DASHBOARD_API_URL", BACKEND);
  vi.stubEnv("DASHBOARD_API_OWNER_TOKEN", OWNER_TOKEN);
  vi.stubEnv("DASHBOARD_API_READ_TOKEN", READ_TOKEN);
  return import("../app/api/dashboard/[...path]/route");
}

function backendOk() {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    void input;
    void init;
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  });
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("dashboard BFF route boundary", () => {
  it("rejects %252e%252e traversal before fetch and never leaks the owner token", async () => {
    const fetchMock = backendOk();
    vi.stubGlobal("fetch", fetchMock);
    const { POST } = await loadRoute();

    // Next decodes each catch-all param once: `%252e%252e` reaches the handler as `%2e%2e`.
    // Before this guard, the later Fetch URL parser decoded those segments to `..` and sent
    // the owner-authenticated request to https://backend.example/orders.
    const req = new NextRequest(
      "https://dashboard.example/api/dashboard/autonomous/%252e%252e/%252e%252e/orders",
      { method: "POST", body: JSON.stringify({ side: "BUY" }) },
    );
    const res = await POST(req, {
      params: Promise.resolve({ path: ["autonomous", "%2e%2e", "%2e%2e", "orders"] }),
    });

    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ detail: "not found" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    ["missing autonomous action", ["autonomous"]],
    ["unknown autonomous action", ["autonomous", "orders"]],
    ["extra autonomous suffix", ["autonomous", "start", "orders"]],
    ["extra emergency-stop suffix", ["emergency-stop", "orders"]],
    ["nested backtest mutation", ["backtests", "run-id"]],
    ["nested dataset mutation", ["research-datasets", "dataset-id"]],
    ["extra risk-config suffix", ["risk-config", "history"]],
    ["encoded slash", ["autonomous", "%2forders"]],
    ["encoded backslash", ["autonomous", "%5corders"]],
    ["deeply nested percent encoding", ["autonomous", "%2525252e%2525252e", "orders"]],
  ])("rejects an inexact write route: %s", async (_label, path) => {
    const fetchMock = backendOk();
    vi.stubGlobal("fetch", fetchMock);
    const { POST } = await loadRoute();
    const req = new NextRequest(`https://dashboard.example/api/dashboard/${path.join("/")}`, {
      method: "POST",
      body: "{}",
    });

    const res = await POST(req, { params: Promise.resolve({ path }) });

    expect(res.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    ["emergency-stop"],
    ["resume"],
    ["risk-config"],
    ["backtests"],
    ["research-datasets"],
    ...["arm", "disarm", "dry_run", "start", "stop", "kill", "reset"].map(
      (action) => ["autonomous", action],
    ),
  ])("allows the exact write route %s", async (...path) => {
    const fetchMock = backendOk();
    vi.stubGlobal("fetch", fetchMock);
    const { POST } = await loadRoute();
    const req = new NextRequest(`https://dashboard.example/api/dashboard/${path.join("/")}`, {
      method: "POST",
      body: "{}",
    });

    const res = await POST(req, { params: Promise.resolve({ path }) });

    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledOnce();
    const [, init] = fetchMock.mock.calls[0];
    expect((init as RequestInit).headers).toMatchObject({ Authorization: `Bearer ${OWNER_TOKEN}` });
  });

  it("encodes safe dynamic segments at the backend forwarding boundary", async () => {
    const fetchMock = backendOk();
    vi.stubGlobal("fetch", fetchMock);
    const { GET } = await loadRoute();
    const req = new NextRequest("https://dashboard.example/api/dashboard/positions/desk%20alpha");

    const res = await GET(req, {
      params: Promise.resolve({ path: ["positions", "desk alpha"] }),
    });

    expect(res.status).toBe(200);
    expect(fetchMock).toHaveBeenCalledWith(
      `${BACKEND}/dashboard/positions/desk%20alpha`,
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({ Authorization: `Bearer ${READ_TOKEN}` }),
      }),
    );
  });
});
