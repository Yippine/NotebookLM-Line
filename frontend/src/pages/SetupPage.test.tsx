import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import SetupPage from "./SetupPage";
import {
  ApiError,
  bindNotebook,
  getChannel,
  getNotebookBinding,
  getPublicCourseAccount,
  recheckNotebookBinding,
  unbindNotebook,
} from "../lib/api";

vi.mock("../lib/api", async () => {
  const actual = await vi.importActual<typeof import("../lib/api")>("../lib/api");
  return {
    ...actual,
    bindNotebook: vi.fn(),
    createChannel: vi.fn(),
    getChannel: vi.fn(),
    getNotebookBinding: vi.fn(),
    getPublicCourseAccount: vi.fn(),
    recheckNotebookBinding: vi.fn(),
    unbindNotebook: vi.fn(),
  };
});

const channel = {
  channel_id: "channel-a",
  notebook_id: null,
  nlm_bound: false,
  webhook_url: "https://example.test/webhook/channel-a",
  binding_status: "unbound" as const,
  notebook_display_name: null,
  last_access_checked_at: null,
};

const unbound = {
  status: "unbound" as const,
  notebook_id: null,
  notebook_title: null,
  last_access_checked_at: null,
};

function renderSetup(): void {
  render(
    <MemoryRouter
      initialEntries={["/setup"]}
      future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
    >
      <Routes>
        <Route path="/" element={<div>邀請碼登入</div>} />
        <Route path="/setup" element={<SetupPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  sessionStorage.setItem("token", "setup-session");
  sessionStorage.setItem("channel_id", "channel-a");
  vi.mocked(getChannel).mockResolvedValue(channel);
  vi.mocked(getPublicCourseAccount).mockResolvedValue({
    email: "course.account@gmail.com",
    health_status: "healthy",
  });
  vi.mocked(getNotebookBinding).mockResolvedValue(unbound);
});

describe("shared Notebook setup page", () => {
  it("shows the no-install Viewer sharing flow without learner cookie upload", async () => {
    renderSetup();

    expect(await screen.findByText("分享自己的 Notebook")).toBeInTheDocument();
    expect(screen.getAllByText("course.account@gmail.com")).toHaveLength(2);
    expect(screen.getByText(/資料可見性/)).toBeInTheDocument();
    expect(screen.getByLabelText("你的 NotebookLM 網址")).toBeInTheDocument();
    expect(screen.queryByText(/storage_state\.json/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Cookie 貼上/i)).not.toBeInTheDocument();
  });

  it("rejects a non-official URL before calling the binding API", async () => {
    const user = userEvent.setup();
    renderSetup();
    const urlInput = await screen.findByLabelText("你的 NotebookLM 網址");

    await user.type(urlInput, "https://evil.example/notebook/Notebook_123456");
    await user.click(screen.getByRole("button", { name: "測試並綁定" }));

    expect(await screen.findByText("請貼上 NotebookLM 官方 HTTPS 網址。")).toBeInTheDocument();
    expect(bindNotebook).not.toHaveBeenCalled();
  });

  it("requires a legacy unbound Notebook ID to use the full URL probe", async () => {
    vi.mocked(getChannel).mockResolvedValue({
      ...channel,
      notebook_id: "LegacyNotebook_123",
      binding_status: "unbound",
    });
    vi.mocked(getNotebookBinding).mockResolvedValue({
      ...unbound,
      notebook_id: "LegacyNotebook_123",
    });

    renderSetup();

    expect(await screen.findByRole("button", { name: "測試並綁定" })).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "重新檢查" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "解除綁定" })).not.toBeInTheDocument();
    });
  });

  it.each([
    ["unbound", "等待分享 Notebook"],
    ["checking", "正在確認讀取與問答功能"],
    ["bound", "NotebookLM 已成功綁定"],
    ["access_revoked", "Notebook 分享可能已取消"],
    ["course_account_unavailable", "需要管理者處理課程帳號"],
    ["error", "NotebookLM 連線發生問題"],
  ] as const)("renders the %s status with an actionable message", async (status, title) => {
    vi.mocked(getChannel).mockResolvedValue({
      ...channel,
      notebook_id: status === "unbound" ? null : "Notebook_123456",
      nlm_bound: status === "bound",
      binding_status: status,
    });
    vi.mocked(getNotebookBinding).mockResolvedValue({
      ...unbound,
      status,
      notebook_id: status === "unbound" ? null : "Notebook_123456",
    });

    renderSetup();

    await waitFor(() => expect(screen.getByText(title)).toBeInTheDocument());
  });

  it("shows the central course account outage instead of a stale bound success", async () => {
    vi.mocked(getChannel).mockResolvedValue({
      ...channel,
      notebook_id: "Notebook_123456",
      nlm_bound: true,
      binding_status: "bound",
    });
    vi.mocked(getNotebookBinding).mockResolvedValue({
      ...unbound,
      status: "bound",
      notebook_id: "Notebook_123456",
    });
    vi.mocked(getPublicCourseAccount).mockResolvedValue({
      email: "course.account@gmail.com",
      health_status: "expired",
    });

    renderSetup();

    expect(await screen.findByText("需要管理者處理課程帳號")).toBeInTheDocument();
    expect(screen.getByText("Notebook 綁定已保留")).toBeInTheDocument();
    expect(screen.queryByText("使用者現在可以在你的 LINE 官方帳號上直接提問。")).not.toBeInTheDocument();
  });

  it("rechecks a revoked binding and then allows it to be unbound", async () => {
    const user = userEvent.setup();
    vi.spyOn(window, "confirm").mockReturnValue(true);
    vi.mocked(getChannel).mockResolvedValue({
      ...channel,
      notebook_id: "Notebook_123456",
      binding_status: "access_revoked",
    });
    vi.mocked(getNotebookBinding).mockResolvedValue({
      status: "access_revoked",
      notebook_id: "Notebook_123456",
      notebook_title: "Learner Notebook",
      last_access_checked_at: null,
    });
    vi.mocked(recheckNotebookBinding).mockResolvedValue({
      status: "bound",
      notebook_id: "Notebook_123456",
      notebook_title: "Learner Notebook",
      last_access_checked_at: "2030-01-01T00:00:00Z",
    });
    vi.mocked(unbindNotebook).mockResolvedValue(unbound);
    renderSetup();

    await user.click(await screen.findByRole("button", { name: "重新檢查" }));
    expect(await screen.findByText("設定完成！")).toBeInTheDocument();
    expect(recheckNotebookBinding).toHaveBeenCalledWith("setup-session", "channel-a");

    await user.click(screen.getByRole("button", { name: "解除綁定" }));
    await waitFor(() => expect(unbindNotebook).toHaveBeenCalledWith("setup-session", "channel-a"));
    expect(await screen.findByText("等待分享 Notebook")).toBeInTheDocument();
  });

  it("returns to invite login when the setup session expires", async () => {
    vi.mocked(getChannel).mockRejectedValue(new ApiError(401, { code: "session_expired" }));
    renderSetup();

    expect(await screen.findByText("邀請碼登入")).toBeInTheDocument();
    expect(sessionStorage.getItem("token")).toBeNull();
  });
});
