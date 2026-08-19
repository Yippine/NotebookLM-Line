import { afterEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  adminLogin,
  adminLogout,
  createChannel,
  exportStudentsCsv,
  setExpiry,
  setSelectedStudentsExpiry,
  setStudentExpiry,
  updateStudentName,
} from "./api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("protected API client", () => {
  it("sends setup credentials only in the Authorization header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ channel_id: "channel-a", webhook_url: "https://example.test/webhook/channel-a" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await createChannel("setup-secret", {
      channel_id: "  channel-a\n",
      channel_secret: "\tline-secret ",
      channel_access_token: " line-token\r\n",
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/channels");
    expect(url).not.toContain("setup-secret");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer setup-secret");
    expect(JSON.parse(String(init.body))).toEqual({
      channel_id: "channel-a",
      channel_secret: "line-secret",
      channel_access_token: "line-token",
    });
  });

  it("submits the admin password in JSON and never in the URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ token: "admin-session", token_type: "bearer", expires_at: "2030-01-01T00:00:00Z" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await adminLogin("admin-password");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/admin/login");
    expect(url).not.toContain("admin-password");
    expect(JSON.parse(String(init.body))).toEqual({ password: "admin-password" });
  });

  it("revokes an admin session with its Bearer token", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ status: "logged_out" }));
    vi.stubGlobal("fetch", fetchMock);

    await adminLogout("admin-session");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/admin/logout");
    expect(init.method).toBe("POST");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer admin-session");
  });

  it("downloads CSV with an admin Bearer token", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("name,code\nLearner,abc", { status: 200, headers: { "Content-Type": "text/csv" } }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const blob = await exportStudentsCsv("admin-session");

    expect(await blob.text()).toContain("Learner");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer admin-session");
  });

  it("cancels the course expiry without deleting bindings", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ expires_at: null }));
    vi.stubGlobal("fetch", fetchMock);

    await setExpiry("admin-session", null);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/admin/set-expiry");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({ expires_at: null, mode: "unassigned" });
  });

  it("sets one learner expiry without changing other channels", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ status: "ok", expires_at: "2030-01-01T00:00:00+00:00" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await setStudentExpiry("admin-session", "channel-one", "2030-01-01T00:00:00Z");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/admin/channels/channel-one/expiry");
    expect(JSON.parse(String(init.body))).toEqual({ expires_at: "2030-01-01T00:00:00Z" });
  });

  it("sets expiry for only the checked learners", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ status: "ok", expires_at: "2030-01-01T00:00:00+00:00", applied_count: 2 }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await setSelectedStudentsExpiry(
      "admin-session",
      ["channel-one", "channel-three"],
      "2030-01-01T00:00:00Z",
    );

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/admin/channels/expiry-batch");
    expect(JSON.parse(String(init.body))).toEqual({
      channel_ids: ["channel-one", "channel-three"],
      expires_at: "2030-01-01T00:00:00Z",
    });
  });

  it("updates the name associated with a manual invite code", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ status: "ok", student_name: "王小明" }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await updateStudentName("admin-session", "manual-code", "王小明");

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/admin/invite-codes/manual-code/name");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(String(init.body))).toEqual({ student_name: "王小明" });
  });

  it("does not echo an unreviewed server error containing secrets", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        jsonResponse({ detail: "storage_state_json SID=top-secret" }, 500),
      ),
    );

    await expect(adminLogin("password")).rejects.toMatchObject({
      message: "系統暫時無法完成操作，請稍後再試。",
    } satisfies Partial<ApiError>);
  });

  it("shows the reviewed Gmail mismatch guidance", () => {
    const error = new ApiError(422, {
      code: "authorization_email_mismatch",
      message: "untrusted upstream detail",
    });

    expect(error.message).toBe(
      "輸入的 Gmail 與授權資料所屬帳號不一致，請改用同一個帳號。",
    );
    expect(error.message).not.toContain("untrusted upstream detail");
  });
});
