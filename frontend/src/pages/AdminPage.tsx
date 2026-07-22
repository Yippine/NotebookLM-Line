import { FormEvent, useEffect, useState } from "react";
import {
  AdminCourseAccount,
  ApiError,
  adminLogin,
  adminLogout,
  checkCourseAccountHealth,
  clearAllBindings,
  configureCourseAccount,
  CourseAccountHealth,
  deleteChannel,
  exportStudentsCsv,
  formatApiError,
  generateInviteCodes,
  getAdminCourseAccount,
  getCourseExpiry,
  getStudents,
  importCsv,
  isSessionExpired,
  reauthenticateCourseAccount,
  setSelectedStudentsExpiry,
  Student,
  updateStudentName,
} from "../lib/api";

type Tab = "students" | "codes" | "account";

const EMPTY_ACCOUNT: AdminCourseAccount = {
  email: null,
  health_status: "unconfigured",
  last_checked_at: null,
  last_success_at: null,
};

const HEALTH_CONTENT: Record<
  CourseAccountHealth,
  { label: string; description: string; className: string }
> = {
  unconfigured: {
    label: "尚未設定",
    description: "請設定課程專用的一般 Gmail 與 NotebookLM 授權。",
    className: "border-warm-200 bg-white/60 text-warm-700",
  },
  healthy: {
    label: "連線正常",
    description: "課程帳號目前可正常存取 NotebookLM。",
    className: "border-emerald-200 bg-emerald-50 text-emerald-800",
  },
  expired: {
    label: "需要重新驗證",
    description: "授權已失效，請在下方提交新的授權 JSON。",
    className: "border-amber-200 bg-amber-50 text-amber-800",
  },
  error: {
    label: "檢查失敗",
    description: "暫時無法確認帳號狀態；請再次檢查，必要時重新驗證。",
    className: "border-red-200 bg-red-50 text-red-800",
  },
};

function formatTime(value: string | null | undefined): string {
  if (!value) return "尚無紀錄";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "尚無紀錄" : date.toLocaleString("zh-TW");
}

function toDateTimeLocal(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function studentBindingLabel(student: Student): { label: string; className: string } {
  switch (student.binding_status) {
    case "bound":
      return { label: "✓ 已綁定", className: "badge-success" };
    case "checking":
      return { label: "檢查中", className: "badge-warning" };
    case "access_revoked":
      return { label: "分享已取消", className: "badge-danger" };
    case "course_account_unavailable":
      return { label: "課程帳號異常", className: "badge-warning" };
    case "error":
      return { label: "檢查失敗", className: "badge-danger" };
    default:
      if (student.nlm_bound) return { label: "✓ 已綁定", className: "badge-success" };
      if (student.channel_id) return { label: "尚未綁定 NLM", className: "badge-warning" };
      return { label: "—", className: "text-warm-400" };
  }
}

export default function AdminPage() {
  const [password, setPassword] = useState("");
  const [adminToken, setAdminToken] = useState(() => sessionStorage.getItem("admin_token") || "");
  const [students, setStudents] = useState<Student[]>([]);
  const [account, setAccount] = useState<AdminCourseAccount>(EMPTY_ACCOUNT);
  const [accountEmail, setAccountEmail] = useState("");
  const [authorizationJson, setAuthorizationJson] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(false);
  const [accountSaving, setAccountSaving] = useState(false);
  const [healthChecking, setHealthChecking] = useState(false);
  const [tab, setTab] = useState<Tab>("students");

  const [genCount, setGenCount] = useState(5);
  const [newCodes, setNewCodes] = useState<string[]>([]);
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [importResult, setImportResult] = useState<{
    imported: number;
    students: Array<{ name: string; code: string }>;
  } | null>(null);
  const [expiryDate, setExpiryDate] = useState("");
  const [courseExpiry, setCourseExpiry] = useState<string | null>(null);
  const [expiryAppliedCount, setExpiryAppliedCount] = useState(0);
  const [expiryTotalCount, setExpiryTotalCount] = useState(0);
  const [selectedChannelIds, setSelectedChannelIds] = useState<string[]>([]);
  const [editingStudentCode, setEditingStudentCode] = useState<string | null>(null);
  const [editingStudentName, setEditingStudentName] = useState("");
  const [studentNameSaving, setStudentNameSaving] = useState(false);

  const logout = (message = "") => {
    sessionStorage.removeItem("admin_token");
    setAdminToken("");
    setPassword("");
    setAuthorizationJson("");
    setStudents([]);
    setAccount(EMPTY_ACCOUNT);
    setAccountEmail("");
    setNewCodes([]);
    setCsvFile(null);
    setImportResult(null);
    setExpiryDate("");
    setCourseExpiry(null);
    setExpiryAppliedCount(0);
    setExpiryTotalCount(0);
    setSelectedChannelIds([]);
    setEditingStudentCode(null);
    setEditingStudentName("");
    setNotice("");
    setError(message);
  };

  const handleLogout = async () => {
    const token = adminToken;
    try {
      if (token) await adminLogout(token);
    } catch {
      // Local secrets must still be cleared if the session already expired or
      // the network is unavailable.
    } finally {
      logout();
    }
  };

  const handleProtectedError = (cause: unknown, fallback: string) => {
    if (isSessionExpired(cause)) {
      logout("管理員工作階段已過期，請重新登入。");
      return;
    }
    setError(formatApiError(cause, fallback));
  };

  const applyStudents = (data: Student[]) => {
    setStudents(data);
    const available = new Set(data.flatMap((student) => student.channel_id ? [student.channel_id] : []));
    setSelectedChannelIds((current) => current.filter((channelId) => available.has(channelId)));
  };

  const applyCourseExpiry = (data: { expires_at: string | null; applied_count: number; total_count: number }) => {
    setCourseExpiry(data.expires_at);
    setExpiryAppliedCount(data.applied_count);
    setExpiryTotalCount(data.total_count);
    setExpiryDate(data.expires_at ? toDateTimeLocal(data.expires_at) : "");
  };

  const applyAccount = (data: AdminCourseAccount) => {
    setAccount(data);
    setAccountEmail(data.email || "");
  };

  const refresh = async (token = adminToken) => {
    if (!token) return;
    setLoading(true);
    setError("");
    try {
      const [studentsResult, accountResult, expiryResult] = await Promise.allSettled([
        getStudents(token),
        getAdminCourseAccount(token),
        getCourseExpiry(token),
      ]);
      if (studentsResult.status === "fulfilled") applyStudents(studentsResult.value);
      else if (isSessionExpired(studentsResult.reason)) return logout("管理員工作階段已過期，請重新登入。");
      else setError(formatApiError(studentsResult.reason, "載入學員資料失敗。"));

      if (accountResult.status === "fulfilled") applyAccount(accountResult.value);
      else if (isSessionExpired(accountResult.reason)) return logout("管理員工作階段已過期，請重新登入。");
      else setError(formatApiError(accountResult.reason, "載入課程帳號狀態失敗。"));

      if (expiryResult.status === "fulfilled") applyCourseExpiry(expiryResult.value);
      else if (isSessionExpired(expiryResult.reason)) return logout("管理員工作階段已過期，請重新登入。");
      else setError(formatApiError(expiryResult.reason, "載入課程到期設定失敗。"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    document.title = "講師管理後台";
    const token = sessionStorage.getItem("admin_token");
    if (token) void refresh(token);
  }, []);

  const handleLogin = async (event: FormEvent) => {
    event.preventDefault();
    if (loading) return;
    setError("");
    setNotice("");
    setLoading(true);
    const submittedPassword = password;
    setPassword("");
    try {
      const response = await adminLogin(submittedPassword);
      // Persist only the short-lived admin session. The password is never persisted.
      sessionStorage.setItem("admin_token", response.token);
      setAdminToken(response.token);
      await refresh(response.token);
    } catch (cause) {
      setError(
        cause instanceof ApiError && cause.statusCode === 401
          ? "管理員密碼不正確。"
          : formatApiError(cause, "登入失敗，請確認密碼後再試。"),
      );
    } finally {
      setLoading(false);
    }
  };

  const handleGenerate = async () => {
    if (!adminToken) return;
    setError("");
    setNotice("");
    setNewCodes([]);
    try {
      const response = await generateInviteCodes(adminToken, genCount);
      setNewCodes(response.codes);
      await refresh();
    } catch (cause) {
      handleProtectedError(cause, "產生邀請碼失敗。");
    }
  };

  const handleDelete = async (channelId: string) => {
    if (!confirm(`確定要刪除 Channel ${channelId} 的綁定？`)) return;
    try {
      await deleteChannel(adminToken, channelId);
      await refresh();
    } catch (cause) {
      handleProtectedError(cause, "刪除 Channel 失敗。");
    }
  };

  const handleImportCsv = async () => {
    if (!csvFile) return;
    setError("");
    setNotice("");
    setImportResult(null);
    try {
      const response = await importCsv(adminToken, csvFile);
      setImportResult(response);
      setCsvFile(null);
      await refresh();
    } catch (cause) {
      handleProtectedError(cause, "匯入 CSV 失敗。");
    }
  };

  const startEditingStudentName = (student: Student) => {
    setEditingStudentCode(student.code);
    setEditingStudentName(student.student_name || "");
  };

  const cancelEditingStudentName = () => {
    setEditingStudentCode(null);
    setEditingStudentName("");
  };

  const handleSaveStudentName = async (student: Student) => {
    if (studentNameSaving || editingStudentName.trim().length > 100) return;
    setStudentNameSaving(true);
    setError("");
    try {
      await updateStudentName(adminToken, student.code, editingStudentName.trim());
      setNotice(editingStudentName.trim() ? "學員姓名已更新。" : "學員姓名已清除。");
      cancelEditingStudentName();
      await refresh();
    } catch (cause) {
      handleProtectedError(cause, "更新學員姓名失敗。");
    } finally {
      setStudentNameSaving(false);
    }
  };

  const toggleChannelSelection = (channelId: string) => {
    setSelectedChannelIds((current) => current.includes(channelId)
      ? current.filter((item) => item !== channelId)
      : [...current, channelId]);
  };

  const toggleAllChannels = () => {
    const available = students
      .filter((student) => student.used && student.channel_id)
      .map((student) => student.channel_id!);
    setSelectedChannelIds((current) => current.length === available.length ? [] : available);
  };

  const handleSetSelectedExpiry = async () => {
    if (!expiryDate || selectedChannelIds.length === 0) return;
    if (!confirm(`確定將選取的 ${selectedChannelIds.length} 位學員設定為此到期時間？`)) return;
    setError("");
    setNotice("");
    try {
      const result = await setSelectedStudentsExpiry(
        adminToken,
        selectedChannelIds,
        new Date(expiryDate).toISOString(),
      );
      setNotice(`已更新選取的 ${result.applied_count} 位學員，其他學員日期沒有變更。`);
      setSelectedChannelIds([]);
      await refresh();
    } catch (cause) {
      handleProtectedError(cause, "設定已勾選學員的到期時間失敗。");
    }
  };

  const handleCancelSelectedExpiry = async () => {
    if (selectedChannelIds.length === 0) return;
    if (!confirm(`確定取消選取的 ${selectedChannelIds.length} 位學員到期限制？`)) return;
    setError("");
    setNotice("");
    try {
      const result = await setSelectedStudentsExpiry(adminToken, selectedChannelIds, null);
      setNotice(`已取消選取的 ${result.applied_count} 位學員到期限制，綁定資料均已保留。`);
      setSelectedChannelIds([]);
      await refresh();
    } catch (cause) {
      handleProtectedError(cause, "取消已勾選學員的到期限制失敗。");
    }
  };

  const handleClearAll = async () => {
    if (!confirm("確定要清除所有學員綁定資料？此操作無法復原。")) return;
    if (!confirm("再次確認：所有 Channel 綁定和 NLM 綁定都會被刪除。")) return;
    try {
      await clearAllBindings(adminToken);
      await refresh();
    } catch (cause) {
      handleProtectedError(cause, "清除綁定失敗。");
    }
  };

  const handleExport = async () => {
    setError("");
    try {
      const blob = await exportStudentsCsv(adminToken);
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = "students-invite-codes.csv";
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(objectUrl);
    } catch (cause) {
      handleProtectedError(cause, "匯出 CSV 失敗。");
    }
  };

  const parseAuthorization = (): Record<string, unknown> | null => {
    try {
      const parsed: unknown = JSON.parse(authorizationJson);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("not-object");
      return parsed as Record<string, unknown>;
    } catch {
      setError("授權 JSON 格式不正確，請確認貼上的是完整 JSON 物件。");
      return null;
    }
  };

  const handleSaveCourseAccount = async (event: FormEvent) => {
    event.preventDefault();
    if (accountSaving) return;
    setError("");
    setNotice("");
    const parsedAuthorization = parseAuthorization();
    if (!parsedAuthorization) return;

    const payload = {
      email: accountEmail.trim(),
      storage_state_json: parsedAuthorization,
      auth_mode: "storage_state" as const,
    };
    // Clear the browser copy before the network request completes; it is never persisted or echoed.
    setAuthorizationJson("");
    setAccountSaving(true);
    try {
      const result = account.email
        ? await reauthenticateCourseAccount(adminToken, payload)
        : await configureCourseAccount(adminToken, payload);
      applyAccount(result);
      setNotice(account.email ? "課程帳號已重新驗證，舊授權已安全替換。" : "課程帳號已設定並通過驗證。");
    } catch (cause) {
      handleProtectedError(cause, "課程帳號驗證失敗；原有有效授權不會被覆蓋。請重新取得授權 JSON 後再試。");
    } finally {
      setAccountSaving(false);
    }
  };

  const handleHealthCheck = async () => {
    if (healthChecking) return;
    setError("");
    setNotice("");
    setHealthChecking(true);
    try {
      const result = await checkCourseAccountHealth(adminToken);
      applyAccount(result);
      setNotice(result.health_status === "healthy" ? "健康檢查完成，課程帳號連線正常。" : "健康檢查完成，請依狀態指引處理。");
    } catch (cause) {
      handleProtectedError(cause, "健康檢查暫時無法完成，請稍後再試。");
    } finally {
      setHealthChecking(false);
    }
  };

  const renderStudentName = (student: Student) => {
    if (editingStudentCode === student.code) {
      return (
        <div className="flex items-center gap-1.5 min-w-52">
          <input
            autoFocus
            value={editingStudentName}
            maxLength={100}
            onChange={(event) => setEditingStudentName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void handleSaveStudentName(student);
              if (event.key === "Escape") cancelEditingStudentName();
            }}
            aria-label="學員姓名"
            className="input-tactile !mb-0 !py-1.5 !px-2 min-w-28 text-sm"
            placeholder="輸入姓名"
          />
          <button onClick={() => void handleSaveStudentName(student)} disabled={studentNameSaving} className="text-xs px-2 py-1.5 rounded-lg bg-copper text-white disabled:opacity-50">儲存</button>
          <button onClick={cancelEditingStudentName} disabled={studentNameSaving} className="text-xs px-2 py-1.5 rounded-lg border border-warm-200 text-warm-600">取消</button>
        </div>
      );
    }
    return (
      <div className="flex items-center gap-2 min-w-28">
        <span className={student.student_name ? "text-warm-700" : "text-warm-400"}>{student.student_name || "未設定"}</span>
        <button onClick={() => startEditingStudentName(student)} className="text-xs text-copper hover:underline">編輯</button>
      </div>
    );
  };

  if (!adminToken) {
    return (
      <div className="min-h-screen flex items-center justify-center p-6">
        <div className="card-raised w-full max-w-sm text-center">
          <div className="mx-auto mb-6 w-14 h-14 rounded-full bg-warm-800 shadow-mid flex items-center justify-center">
            <svg className="w-7 h-7 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2} aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
            </svg>
          </div>
          <h1 className="font-display text-2xl font-bold text-warm-800 mb-2">講師管理後台</h1>
          <p className="text-sm text-warm-500 mb-6">密碼只用來交換短效工作階段，不會保存在瀏覽器。</p>
          <form onSubmit={handleLogin} className="space-y-4">
            <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="管理員密碼" required autoComplete="current-password" className="input-tactile text-center" />
            {error && <div role="alert" className="px-4 py-2.5 rounded-tactile bg-red-50 border border-red-200 text-red-700 text-sm">{error}</div>}
            <button type="submit" disabled={loading} className="btn-primary">{loading ? "登入中..." : "登入"}</button>
          </form>
        </div>
      </div>
    );
  }

  const active = students.filter((student) => student.used);
  const unused = students.filter((student) => !student.used);
  const activeChannelIds = active.flatMap((student) => student.channel_id ? [student.channel_id] : []);
  const allChannelsSelected = activeChannelIds.length > 0
    && activeChannelIds.every((channelId) => selectedChannelIds.includes(channelId));
  const someChannelsSelected = selectedChannelIds.length > 0 && !allChannelsSelected;
  const health = HEALTH_CONTENT[account.health_status];

  return (
    <div className="min-h-screen p-6">
      <div className="max-w-4xl mx-auto space-y-6">
        <div className="flex items-center justify-between gap-3">
          <h1 className="font-display text-2xl font-bold text-warm-800">講師管理後台</h1>
          <div className="flex gap-2">
            <button onClick={() => void refresh()} disabled={loading} className="btn-ghost text-sm">{loading ? "載入中..." : "🔄 重新整理"}</button>
            <button onClick={() => void handleLogout()} className="btn-ghost text-sm">登出</button>
          </div>
        </div>

        {error && <div role="alert" className="px-4 py-2.5 rounded-tactile bg-red-50 border border-red-200 text-red-700 text-sm">{error}</div>}
        {notice && <div role="status" className="px-4 py-2.5 rounded-tactile bg-emerald-50 border border-emerald-200 text-emerald-700 text-sm">{notice}</div>}

        <div className="flex rounded-tactile bg-surface-inset shadow-inset p-1 gap-1 overflow-x-auto">
          {([
            ["students", "👥 學員管理"],
            ["codes", "🎟️ 邀請碼管理"],
            ["account", "📚 NotebookLM 帳號"],
          ] as const).map(([key, label]) => (
            <button
              key={key}
              onClick={() => { setTab(key); setError(""); setNotice(""); }}
              className={`flex-1 min-w-max py-2.5 px-3 rounded-lg text-sm font-medium transition-all ${
                tab === key ? "bg-surface-raised shadow-soft text-warm-800" : "text-warm-500 hover:text-warm-700"
              }`}
            >
              {label}
            </button>
          ))}
        </div>

        {tab === "students" && (
          <div className="space-y-6">
            <div className="card-raised">
              <h2 className="font-display text-lg font-bold text-warm-800 mb-4">⏰ 課程到期設定</h2>
              <p className="text-sm text-warm-500 mb-2">先在下方勾選學員，再選擇日期並套用。勾選表格左上角可以全選目前全部學員。</p>
              <p className="text-sm text-red-600 mb-3">到期後只會永久刪除已勾選並套用該日期的學員資料；其他學員不受影響。</p>
              <div className="flex items-center gap-3 flex-wrap">
                <input type="datetime-local" value={expiryDate} onChange={(event) => setExpiryDate(event.target.value)} className="input-tactile w-auto !mb-0" />
                <button onClick={() => void handleSetSelectedExpiry()} disabled={!expiryDate || selectedChannelIds.length === 0} className="btn-copper">套用至已勾選學員（{selectedChannelIds.length}）</button>
                <button onClick={() => void handleCancelSelectedExpiry()} disabled={selectedChannelIds.length === 0} className="text-sm px-4 py-2.5 rounded-tactile border border-red-200 text-red-600 hover:bg-red-50 disabled:opacity-50">取消已勾選到期</button>
              </div>
              <p className="text-xs text-warm-500 mt-2">
                {courseExpiry
                  ? `目前選擇日期：${new Date(courseExpiry).toLocaleString("zh-TW", { timeZone: "Asia/Taipei" })}；已有 ${expiryAppliedCount}/${expiryTotalCount} 位設定到期時間（日期可能不同）。`
                  : "目前狀態：未設定到期時間（不限期）"}
              </p>
            </div>

            <div className="card-raised">
              <div className="flex items-center justify-between mb-4 gap-3">
                <h2 className="font-display text-lg font-bold text-warm-800">學員綁定狀況 <span className="ml-2 badge bg-copper/10 text-copper border border-copper/20">{active.length}</span></h2>
                <button onClick={() => void handleClearAll()} className="text-xs px-3 py-1.5 rounded-lg border border-red-200 text-red-600 hover:bg-red-50">清除全部綁定</button>
              </div>
              {active.length === 0 ? (
                <p className="text-warm-400 text-sm text-center py-8">尚無學員完成綁定</p>
              ) : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead><tr className="border-b-2 border-warm-200 text-left">
                      <th className="pb-3 pr-3 w-10"><input type="checkbox" aria-label="全選學員" checked={allChannelsSelected} ref={(element) => { if (element) element.indeterminate = someChannelsSelected; }} onChange={toggleAllChannels} className="h-4 w-4 accent-copper" /></th>
                      <th className="pb-3 font-semibold text-warm-600 text-xs uppercase tracking-wider">姓名</th>
                      <th className="pb-3 font-semibold text-warm-600 text-xs uppercase tracking-wider">邀請碼</th>
                      <th className="pb-3 font-semibold text-warm-600 text-xs uppercase tracking-wider">Channel ID</th>
                      <th className="pb-3 font-semibold text-warm-600 text-xs uppercase tracking-wider">NLM 狀態</th>
                      <th className="pb-3 font-semibold text-warm-600 text-xs uppercase tracking-wider">到期時間</th>
                      <th className="pb-3 font-semibold text-warm-600 text-xs uppercase tracking-wider">操作</th>
                    </tr></thead>
                    <tbody>{active.map((student) => {
                      const bindingLabel = studentBindingLabel(student);
                      return (
                        <tr key={student.code} className="border-b border-warm-200/60 hover:bg-surface-inset/50">
                          <td className="py-3 pr-3"><input type="checkbox" aria-label={`選取 ${student.student_name || student.channel_id || student.code}`} checked={Boolean(student.channel_id && selectedChannelIds.includes(student.channel_id))} disabled={!student.channel_id} onChange={() => student.channel_id && toggleChannelSelection(student.channel_id)} className="h-4 w-4 accent-copper" /></td>
                          <td className="py-3">{renderStudentName(student)}</td>
                          <td className="py-3 font-mono text-warm-700 text-xs">{student.code}</td>
                          <td className="py-3 font-mono text-warm-700 text-xs">{student.channel_id || "—"}</td>
                          <td className="py-3"><span className={bindingLabel.className}>{bindingLabel.label}</span></td>
                          <td className="py-3 text-xs text-warm-600">{student.expires_at ? new Date(student.expires_at).toLocaleString("zh-TW", { timeZone: "Asia/Taipei" }) : "未設定"}</td>
                          <td className="py-3">{student.channel_id && <button onClick={() => void handleDelete(student.channel_id!)} className="text-xs px-2 py-1.5 rounded-lg border border-red-200 text-red-600 hover:bg-red-50">刪除</button>}</td>
                        </tr>
                      );
                    })}</tbody>
                  </table>
                </div>
              )}
            </div>
          </div>
        )}

        {tab === "codes" && (
          <div className="space-y-6">
            <div className="card-raised">
              <h2 className="font-display text-lg font-bold text-warm-800 mb-4">📋 匯入學員名單</h2>
              <p className="text-sm text-warm-500 mb-3">上傳 CSV 檔案（需包含「姓名」或「name」欄位），系統自動為每位學員產生邀請碼。</p>
              <div className="flex items-center gap-3 flex-wrap">
                <input type="file" accept=".csv" onChange={(event) => setCsvFile(event.target.files?.[0] || null)} className="text-sm text-warm-500 file:mr-3 file:py-2 file:px-4 file:rounded-tactile file:border-0 file:text-sm file:font-medium file:bg-surface-inset file:text-warm-700 file:shadow-soft file:cursor-pointer" />
                <button onClick={() => void handleImportCsv()} disabled={!csvFile} className="btn-copper">匯入</button>
              </div>
              {importResult && <p className="text-sm text-warm-600 mt-4"><span className="badge-success">✓</span><span className="ml-2">已匯入 {importResult.imported} 位學員</span></p>}
            </div>

            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <div className="card-raised">
                <h2 className="font-display text-lg font-bold text-warm-800 mb-4">🎟️ 手動產生邀請碼</h2>
                <div className="flex items-center gap-3">
                  <span className="text-sm text-warm-600">數量：</span>
                  <input type="number" min={1} max={100} value={genCount} onChange={(event) => setGenCount(Number(event.target.value))} className="input-tactile w-24 !mb-0 text-center" />
                  <button onClick={() => void handleGenerate()} className="btn-copper">產生</button>
                </div>
                {newCodes.length > 0 && (
                  <div className="mt-4">
                    <p className="text-sm text-warm-600 mb-2"><span className="badge-success">✓</span><span className="ml-2">已產生 {newCodes.length} 組邀請碼</span></p>
                    <textarea readOnly value={newCodes.join("\n")} className="input-tactile h-24 font-mono text-sm resize-none cursor-text" onClick={(event) => event.currentTarget.select()} />
                  </div>
                )}
              </div>
              <div className="card-raised flex flex-col justify-between">
                <h2 className="font-display text-lg font-bold text-warm-800 mb-4">📥 匯出</h2>
                <button onClick={() => void handleExport()} className="btn-ghost text-sm text-center">匯出學員 ↔ 邀請碼對照表 CSV</button>
              </div>
            </div>

            <div className="card-raised">
              <h2 className="font-display text-lg font-bold text-warm-800 mb-4">未使用的邀請碼 <span className="ml-2 badge bg-warm-200 text-warm-600 border border-warm-300">{unused.length}</span></h2>
              {unused.length === 0 ? <p className="text-warm-400 text-sm text-center py-4">沒有未使用的邀請碼</p> : (
                <div className="overflow-x-auto"><table className="w-full text-sm">
                  <thead><tr className="border-b-2 border-warm-200 text-left"><th className="pb-3 font-semibold text-warm-600 text-xs uppercase tracking-wider">姓名</th><th className="pb-3 font-semibold text-warm-600 text-xs uppercase tracking-wider">邀請碼</th></tr></thead>
                  <tbody>{unused.map((student) => <tr key={student.code} className="border-b border-warm-200/60 hover:bg-surface-inset/50"><td className="py-2">{renderStudentName(student)}</td><td className="py-2 font-mono text-warm-600 text-xs">{student.code}</td></tr>)}</tbody>
                </table></div>
              )}
            </div>
          </div>
        )}

        {tab === "account" && (
          <div className="space-y-6">
            <div className="card-raised">
              <div className="flex items-start justify-between gap-4 flex-wrap">
                <div>
                  <h2 className="font-display text-lg font-bold text-warm-800">課程 NotebookLM 帳號</h2>
                  <p className="text-sm text-warm-500 mt-1">這是一個課程專用的一般 Gmail，不是 Google Workspace 或 Service Account。</p>
                </div>
                <button onClick={() => void handleHealthCheck()} disabled={healthChecking || account.health_status === "unconfigured"} className="btn-copper">
                  {healthChecking ? "檢查中..." : "立即檢查"}
                </button>
              </div>
              <div className={`rounded-tactile border p-4 mt-5 ${health.className}`}>
                <div className="flex items-center justify-between gap-3"><strong>{health.label}</strong>{account.email && <span className="font-mono text-sm break-all">{account.email}</span>}</div>
                <p className="text-sm mt-1">{health.description}</p>
                <dl className="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-4 text-xs">
                  <div><dt className="opacity-70">最後檢查</dt><dd>{formatTime(account.last_checked_at)}</dd></div>
                  <div><dt className="opacity-70">最後成功</dt><dd>{formatTime(account.last_success_at)}</dd></div>
                </dl>
              </div>
            </div>

            <form onSubmit={handleSaveCourseAccount} className="card-raised space-y-4">
              <div>
                <h2 className="font-display text-lg font-bold text-warm-800">{account.email ? "設定其他 Gmail 或重新驗證" : "設定課程 Gmail"}</h2>
                <p className="text-sm text-warm-500 mt-1">系統會先驗證新授權；失敗時不會覆蓋目前可用的授權與學員綁定。</p>
              </div>
              <div>
                <label htmlFor="course-email" className="block text-xs font-semibold text-warm-600 uppercase tracking-wider mb-2">課程 Gmail</label>
                <input id="course-email" type="email" value={accountEmail} onChange={(event) => setAccountEmail(event.target.value)} required autoComplete="off" className="input-tactile" placeholder="course.account@gmail.com" />
              </div>
              <div>
                <label htmlFor="course-authorization" className="block text-xs font-semibold text-warm-600 uppercase tracking-wider mb-2">NotebookLM 授權 JSON</label>
                <textarea
                  id="course-authorization"
                  value={authorizationJson}
                  onChange={(event) => setAuthorizationJson(event.target.value)}
                  required
                  spellCheck={false}
                  autoComplete="off"
                  className="input-tactile min-h-36 font-mono text-sm resize-y"
                  placeholder="只在送出前暫時貼在這裡"
                />
                <p className="text-xs text-warm-500 mt-2">授權內容不會被回顯或保存於瀏覽器，送出時會立即從欄位清除；伺服器只會加密保存驗證成功的授權。</p>
              </div>
              <div className="rounded-lg bg-amber-50 border border-amber-200 px-4 py-3 text-sm text-amber-900">
                請使用專供本課程的 Gmail。變更 Gmail 後，學員必須將 Notebook 重新分享給新帳號，再執行重新檢查。
              </div>
              <button type="submit" disabled={accountSaving || !accountEmail.trim() || !authorizationJson.trim()} className="btn-primary">
                {accountSaving ? "正在安全驗證..." : account.email ? "驗證並更新課程帳號" : "驗證並儲存課程帳號"}
              </button>
            </form>
          </div>
        )}
      </div>
    </div>
  );
}
