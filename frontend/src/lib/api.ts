const BASE = "/api";

export type NotebookBindingStatus =
  | "unbound"
  | "checking"
  | "bound"
  | "access_revoked"
  | "course_account_unavailable"
  | "error";

export type CourseAccountHealth = "unconfigured" | "healthy" | "expired" | "error";

export type NotebookBinding = {
  status: NotebookBindingStatus;
  notebook_id: string | null;
  notebook_title: string | null;
  last_access_checked_at: string | null;
  request_id?: string | null;
};

export type PublicCourseAccount = {
  email: string | null;
  health_status: CourseAccountHealth;
};

export type AdminCourseAccount = PublicCourseAccount & {
  auth_mode?: "storage_state" | null;
  last_checked_at?: string | null;
  last_success_at?: string | null;
  error_code?: string | null;
};

export type Student = {
  code: string;
  student_name: string;
  used: boolean;
  channel_id: string | null;
  nlm_bound: boolean;
  notebook_id: string | null;
  binding_status?: NotebookBindingStatus | null;
  notebook_display_name?: string | null;
  last_access_checked_at?: string | null;
  expires_at: string | null;
  created_at: string;
  invite_expires_at?: string | null;
};

export type CourseExpiry = {
  expires_at: string | null;
  applied_count: number;
  total_count: number;
};

type ApiErrorBody = {
  code?: string;
  status?: string;
  message?: string;
  request_id?: string;
};

const ERROR_MESSAGES: Record<string, string> = {
  invalid_notebook_url: "請貼上有效的 NotebookLM 官方網址。",
  notebook_not_shared: "課程帳號目前無法讀取這本 Notebook，請確認已設為檢視者並完成私人分享。",
  not_shared: "課程帳號目前無法讀取這本 Notebook，請確認已設為檢視者並完成私人分享。",
  notebook_access_denied: "課程帳號目前無法讀取這本 Notebook，請重新分享後再試。",
  notebook_not_found: "找不到這本 Notebook，請確認網址與分享設定。",
  access_revoked: "Notebook 的分享權限已取消，請重新分享給課程帳號。",
  course_account_unavailable: "課程 NotebookLM 帳號暫時無法使用，請聯繫管理者。",
  course_account_expired: "課程 NotebookLM 帳號需要重新驗證，請聯繫管理者。",
  course_auth_expired: "課程 NotebookLM 帳號需要重新驗證，請聯繫管理者。",
  course_account_unconfigured: "課程 NotebookLM 帳號尚未完成設定，請聯繫管理者。",
  course_authorization_unavailable: "課程 NotebookLM 帳號暫時無法使用，請聯繫管理者。",
  invalid_authorization: "授權資料格式不正確，請重新取得完整的授權 JSON。",
  authorization_email_mismatch: "輸入的 Gmail 與授權資料所屬帳號不一致，請改用同一個帳號。",
  unsupported_auth_mode: "目前不支援這種授權格式。",
  binding_probe_required: "此 Channel 尚未完成新版綁定，請貼上 Notebook 網址並執行測試綁定。",
  binding_check_in_progress: "Notebook 正在檢查中，請稍後再試。",
  binding_changed: "Notebook 綁定狀態已變更，請重新整理後再試。",
  course_account_changed: "課程帳號剛剛已更新，請重新執行檢查。",
  query_capacity_exceeded: "目前查詢人數較多，請稍後再試。",
  upstream_timeout: "NotebookLM 回應逾時，請稍後再試。",
  upstream_unavailable: "NotebookLM 服務暫時無法連線，請稍後再試。",
  rate_limited: "操作次數過多，請稍後再試。",
  session_expired: "工作階段已過期，請重新登入。",
  invalid_session: "工作階段無效，請重新登入。",
  invalid_credentials: "登入資料不正確。",
  invite_code_in_use: "已使用的邀請碼不可刪除，請從學員綁定狀況刪除。",
};

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function parseErrorBody(value: unknown): ApiErrorBody {
  if (!isObject(value)) return {};
  const detail = isObject(value.detail) ? value.detail : value;
  return {
    code: typeof detail.code === "string" ? detail.code : undefined,
    status: typeof detail.status === "string" ? detail.status : undefined,
    message: typeof detail.message === "string" ? detail.message : undefined,
    request_id: typeof detail.request_id === "string" ? detail.request_id : undefined,
  };
}

function defaultErrorMessage(status: number): string {
  if (status === 400 || status === 422) return "送出的資料格式不正確，請檢查後再試。";
  if (status === 401) return "工作階段已過期，請重新登入。";
  if (status === 403) return "你沒有權限執行這項操作。";
  if (status === 404) return "找不到要求的資料。";
  if (status === 409) return "資料狀態已變更，請重新整理後再試。";
  if (status === 429) return "操作次數過多，請稍後再試。";
  return "系統暫時無法完成操作，請稍後再試。";
}

export class ApiError extends Error {
  readonly statusCode: number;
  readonly code?: string;
  readonly requestId?: string;
  readonly resourceStatus?: string;

  constructor(statusCode: number, body: ApiErrorBody) {
    // Only render reviewed messages. Never echo an upstream exception or response body.
    super((body.code && ERROR_MESSAGES[body.code]) || defaultErrorMessage(statusCode));
    this.name = "ApiError";
    this.statusCode = statusCode;
    this.code = body.code;
    this.requestId = body.request_id;
    this.resourceStatus = body.status;
  }
}

async function throwApiError(res: Response): Promise<never> {
  let body: ApiErrorBody = {};
  try {
    body = parseErrorBody(await res.json());
  } catch {
    // Intentionally ignore raw response text because it may contain upstream secrets.
  }
  throw new ApiError(res.status, body);
}

async function handleResponse<T>(res: Response): Promise<T> {
  if (!res.ok) return throwApiError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

function bearer(token: string, json = false): HeadersInit {
  return {
    Authorization: `Bearer ${token}`,
    ...(json ? { "Content-Type": "application/json" } : {}),
  };
}

export function formatApiError(error: unknown, fallback = "系統暫時無法完成操作，請稍後再試。"): string {
  if (!(error instanceof ApiError)) return fallback;
  return error.requestId ? `${error.message}（查詢編號：${error.requestId}）` : error.message;
}

export function isSessionExpired(error: unknown): boolean {
  return error instanceof ApiError && error.statusCode === 401;
}

export async function verifyInvite(code: string): Promise<{
  token: string;
  token_type: "bearer";
  expires_at: string;
  channel_id?: string;
}> {
  const res = await fetch(`${BASE}/verify-invite`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
  return handleResponse(res);
}

export async function createChannel(
  token: string,
  data: { channel_id: string; channel_secret: string; channel_access_token: string },
): Promise<{ channel_id: string; webhook_url: string }> {
  const normalized = {
    channel_id: data.channel_id.trim(),
    channel_secret: data.channel_secret.trim(),
    channel_access_token: data.channel_access_token.trim(),
  };
  const res = await fetch(`${BASE}/channels`, {
    method: "POST",
    headers: bearer(token, true),
    body: JSON.stringify(normalized),
  });
  return handleResponse(res);
}

export async function getChannel(
  token: string,
  channelId: string,
): Promise<{
  channel_id: string;
  notebook_id: string | null;
  nlm_bound: boolean;
  webhook_url: string;
  binding_status?: NotebookBindingStatus | null;
  notebook_display_name?: string | null;
  last_access_checked_at?: string | null;
}> {
  const res = await fetch(`${BASE}/channels/${encodeURIComponent(channelId)}`, {
    headers: bearer(token),
  });
  return handleResponse(res);
}

export async function getPublicCourseAccount(token: string): Promise<PublicCourseAccount> {
  const res = await fetch(`${BASE}/course-account/public`, { headers: bearer(token) });
  return handleResponse(res);
}

export async function getNotebookBinding(token: string, channelId: string): Promise<NotebookBinding> {
  const res = await fetch(`${BASE}/channels/${encodeURIComponent(channelId)}/notebook-binding`, {
    headers: bearer(token),
  });
  return handleResponse(res);
}

export async function bindNotebook(
  token: string,
  channelId: string,
  notebookUrl: string,
): Promise<NotebookBinding> {
  const res = await fetch(`${BASE}/channels/${encodeURIComponent(channelId)}/notebook-binding`, {
    method: "POST",
    headers: bearer(token, true),
    body: JSON.stringify({ notebook_url: notebookUrl }),
  });
  return handleResponse(res);
}

export async function recheckNotebookBinding(token: string, channelId: string): Promise<NotebookBinding> {
  const res = await fetch(`${BASE}/channels/${encodeURIComponent(channelId)}/notebook-binding/recheck`, {
    method: "POST",
    headers: bearer(token),
  });
  return handleResponse(res);
}

export async function unbindNotebook(token: string, channelId: string): Promise<NotebookBinding> {
  const res = await fetch(`${BASE}/channels/${encodeURIComponent(channelId)}/notebook-binding`, {
    method: "DELETE",
    headers: bearer(token),
  });
  return handleResponse(res);
}

// --- Admin APIs ---

export async function adminLogin(password: string): Promise<{
  token: string;
  token_type: "bearer";
  expires_at: string;
}> {
  const res = await fetch(`${BASE}/admin/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  return handleResponse(res);
}

export async function adminLogout(adminToken: string): Promise<void> {
  const res = await fetch(`${BASE}/admin/logout`, {
    method: "POST",
    headers: bearer(adminToken),
  });
  return handleResponse(res);
}

export async function getStudents(adminToken: string): Promise<Student[]> {
  const res = await fetch(`${BASE}/admin/students`, { headers: bearer(adminToken) });
  return handleResponse(res);
}

export async function updateStudentName(
  adminToken: string,
  inviteCode: string,
  studentName: string,
): Promise<{ status: string; student_name: string }> {
  const res = await fetch(`${BASE}/admin/invite-codes/${encodeURIComponent(inviteCode)}/name`, {
    method: "PUT",
    headers: bearer(adminToken, true),
    body: JSON.stringify({ student_name: studentName }),
  });
  return handleResponse(res);
}

export async function deleteInviteCode(
  adminToken: string,
  inviteCode: string,
): Promise<void> {
  const res = await fetch(`${BASE}/admin/invite-codes/${encodeURIComponent(inviteCode)}`, {
    method: "DELETE",
    headers: bearer(adminToken),
  });
  return handleResponse(res);
}

export async function generateInviteCodes(adminToken: string, count: number): Promise<{ codes: string[] }> {
  const res = await fetch(`${BASE}/invite-codes/generate?count=${encodeURIComponent(String(count))}`, {
    method: "POST",
    headers: bearer(adminToken),
  });
  return handleResponse(res);
}

export async function deleteChannel(adminToken: string, channelId: string): Promise<void> {
  const res = await fetch(`${BASE}/admin/channels/${encodeURIComponent(channelId)}`, {
    method: "DELETE",
    headers: bearer(adminToken),
  });
  return handleResponse(res);
}

export async function importCsv(
  adminToken: string,
  file: File,
): Promise<{ imported: number; students: Array<{ name: string; code: string }> }> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${BASE}/admin/import-csv`, {
    method: "POST",
    headers: bearer(adminToken),
    body: form,
  });
  return handleResponse(res);
}

export async function getCourseExpiry(adminToken: string): Promise<CourseExpiry> {
  const res = await fetch(`${BASE}/admin/set-expiry`, { headers: bearer(adminToken) });
  return handleResponse(res);
}

export async function setExpiry(
  adminToken: string,
  expiresAt: string | null,
  mode: "unassigned" | "all" = "unassigned",
): Promise<CourseExpiry> {
  const res = await fetch(`${BASE}/admin/set-expiry`, {
    method: "PUT",
    headers: bearer(adminToken, true),
    body: JSON.stringify({ expires_at: expiresAt, mode }),
  });
  return handleResponse(res);
}

export async function setStudentExpiry(
  adminToken: string,
  channelId: string,
  expiresAt: string | null,
): Promise<{ status: string; expires_at: string | null }> {
  const res = await fetch(`${BASE}/admin/channels/${encodeURIComponent(channelId)}/expiry`, {
    method: "PUT",
    headers: bearer(adminToken, true),
    body: JSON.stringify({ expires_at: expiresAt }),
  });
  return handleResponse(res);
}

export async function setSelectedStudentsExpiry(
  adminToken: string,
  channelIds: string[],
  expiresAt: string | null,
): Promise<{ status: string; expires_at: string | null; applied_count: number }> {
  const res = await fetch(`${BASE}/admin/channels/expiry-batch`, {
    method: "PUT",
    headers: bearer(adminToken, true),
    body: JSON.stringify({ channel_ids: channelIds, expires_at: expiresAt }),
  });
  return handleResponse(res);
}

export async function clearAllBindings(adminToken: string): Promise<void> {
  const res = await fetch(`${BASE}/admin/clear-all`, {
    method: "DELETE",
    headers: bearer(adminToken),
  });
  return handleResponse(res);
}

export async function exportStudentsCsv(adminToken: string): Promise<Blob> {
  const res = await fetch(`${BASE}/admin/export-csv`, { headers: bearer(adminToken) });
  if (!res.ok) return throwApiError(res);
  return res.blob();
}

export async function getAdminCourseAccount(adminToken: string): Promise<AdminCourseAccount> {
  const res = await fetch(`${BASE}/admin/course-account`, { headers: bearer(adminToken) });
  return handleResponse(res);
}

type CourseAccountCredential = {
  email: string;
  storage_state_json: Record<string, unknown>;
  auth_mode: "storage_state";
};

export async function configureCourseAccount(
  adminToken: string,
  data: CourseAccountCredential,
): Promise<AdminCourseAccount> {
  const res = await fetch(`${BASE}/admin/course-account`, {
    method: "PUT",
    headers: bearer(adminToken, true),
    body: JSON.stringify(data),
  });
  return handleResponse(res);
}

export async function reauthenticateCourseAccount(
  adminToken: string,
  data: CourseAccountCredential,
): Promise<AdminCourseAccount> {
  const res = await fetch(`${BASE}/admin/course-account/reauthenticate`, {
    method: "POST",
    headers: bearer(adminToken, true),
    body: JSON.stringify(data),
  });
  return handleResponse(res);
}

export async function checkCourseAccountHealth(adminToken: string): Promise<AdminCourseAccount> {
  const res = await fetch(`${BASE}/admin/course-account/health-check`, {
    method: "POST",
    headers: bearer(adminToken),
  });
  return handleResponse(res);
}
