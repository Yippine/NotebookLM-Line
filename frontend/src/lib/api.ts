const BASE = "/api";

async function handleResponse<T>(res: Response): Promise<T> {
  const text = await res.text();
  if (!res.ok) {
    let message = `錯誤 ${res.status}`;
    try {
      const json = JSON.parse(text);
      message = json.detail || message;
    } catch {
      message = text || message;
    }
    throw new Error(message);
  }
  return JSON.parse(text);
}

export async function verifyInvite(code: string): Promise<{ token: string; channel_id?: string }> {
  const res = await fetch(`${BASE}/verify-invite`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
  return handleResponse(res);
}

export async function createChannel(
  token: string,
  data: { channel_id: string; channel_secret: string; channel_access_token: string }
): Promise<{ channel_id: string; webhook_url: string }> {
  const res = await fetch(`${BASE}/channels?token=${token}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  return handleResponse(res);
}

export async function nlmLogin(
  channelId: string,
  storageStateJson: object
): Promise<{ status: string; notebook_id: string | null; message: string }> {
  const res = await fetch(`${BASE}/channels/${channelId}/nlm-login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ storage_state_json: storageStateJson }),
  });
  return handleResponse(res);
}

export async function nlmBindLocal(
  channelId: string
): Promise<{ status: string; notebook_id: string | null; message: string }> {
  const res = await fetch(`${BASE}/channels/${channelId}/nlm-bind-local`, {
    method: "POST",
  });
  return handleResponse(res);
}

export async function getNlmStatus(
  channelId: string
): Promise<{ bound: boolean; notebook_id: string | null; login_status?: string }> {
  const res = await fetch(`${BASE}/channels/${channelId}/nlm-status`);
  return handleResponse(res);
}

export async function getChannel(
  token: string,
  channelId: string
): Promise<{ channel_id: string; notebook_id: string | null; nlm_bound: boolean; webhook_url: string }> {
  const res = await fetch(`${BASE}/channels/${channelId}?token=${token}`);
  return handleResponse(res);
}

export async function getNotebooks(
  channelId: string
): Promise<{ notebooks: Array<{ id: string; title: string }> }> {
  const res = await fetch(`${BASE}/channels/${channelId}/notebooks`);
  return handleResponse(res);
}

export async function selectNotebook(
  channelId: string,
  notebookId: string
): Promise<{ status: string; notebook_id: string }> {
  const res = await fetch(`${BASE}/channels/${channelId}/notebook`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notebook_id: notebookId }),
  });
  return handleResponse(res);
}

// --- Admin APIs ---

export async function adminLogin(password: string): Promise<void> {
  // Just verify password works by calling students list
  const res = await fetch(`${BASE}/admin/students?admin_password=${encodeURIComponent(password)}`);
  if (!res.ok) throw new Error("密碼錯誤");
}

export async function getStudents(password: string): Promise<
  Array<{
    code: string;
    used: boolean;
    channel_id: string | null;
    nlm_bound: boolean;
    notebook_id: string | null;
    created_at: string;
  }>
> {
  const res = await fetch(`${BASE}/admin/students?admin_password=${encodeURIComponent(password)}`);
  return handleResponse(res);
}

export async function generateInviteCodes(password: string, count: number): Promise<{ codes: string[] }> {
  const res = await fetch(`${BASE}/invite-codes/generate?count=${count}&admin_password=${encodeURIComponent(password)}`, {
    method: "POST",
  });
  return handleResponse(res);
}

export async function deleteChannel(password: string, channelId: string): Promise<void> {
  const res = await fetch(`${BASE}/admin/channels/${channelId}?admin_password=${encodeURIComponent(password)}`, {
    method: "DELETE",
  });
  return handleResponse(res);
}

export async function importCsv(password: string, file: File): Promise<{ imported: number; students: Array<{ name: string; code: string }> }> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch(`${BASE}/admin/import-csv?admin_password=${encodeURIComponent(password)}`, {
    method: "POST",
    body: form,
  });
  return handleResponse(res);
}

export async function setExpiry(password: string, expiresAt: string): Promise<void> {
  const res = await fetch(`${BASE}/admin/set-expiry?admin_password=${encodeURIComponent(password)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expires_at: expiresAt }),
  });
  return handleResponse(res);
}

export async function clearAllBindings(password: string): Promise<void> {
  const res = await fetch(`${BASE}/admin/clear-all?admin_password=${encodeURIComponent(password)}`, {
    method: "DELETE",
  });
  return handleResponse(res);
}

export function exportCsvUrl(password: string): string {
  return `${BASE}/admin/export-csv?admin_password=${encodeURIComponent(password)}`;
}
