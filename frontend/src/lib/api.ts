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

export async function verifyInvite(code: string): Promise<{ token: string }> {
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
