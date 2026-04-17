import { useState, FormEvent, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { createChannel, nlmLogin, nlmBindLocal, getNlmStatus } from "../lib/api";

export default function SetupPage() {
  const navigate = useNavigate();
  const token = sessionStorage.getItem("token") || "";

  const [channelId, setChannelId] = useState("");
  const [channelSecret, setChannelSecret] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [step, setStep] = useState<"channel" | "nlm" | "done">("channel");
  const [nlmJson, setNlmJson] = useState("");
  const [nlmStatus, setNlmStatus] = useState<{ bound: boolean; notebook_id: string | null }>({ bound: false, notebook_id: null });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [bindMode, setBindMode] = useState<"auto" | "manual">("auto");

  useEffect(() => {
    if (!token) navigate("/");
  }, [token, navigate]);

  const handleChannelSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const res = await createChannel(token, {
        channel_id: channelId,
        channel_secret: channelSecret,
        channel_access_token: accessToken,
      });
      setWebhookUrl(res.webhook_url);
      setStep("nlm");
    } catch (err) {
      setError(err instanceof Error ? err.message : "建立失敗");
    } finally {
      setLoading(false);
    }
  };

  const handleAutoBind = async () => {
    setError("");
    setLoading(true);
    try {
      await nlmBindLocal(channelId);
      const status = await getNlmStatus(channelId);
      setNlmStatus(status);
      setStep("done");
    } catch (err) {
      setError(err instanceof Error ? err.message : "綁定失敗");
    } finally {
      setLoading(false);
    }
  };

  const handleManualBind = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const parsed = JSON.parse(nlmJson);
      await nlmLogin(channelId, parsed);
      const status = await getNlmStatus(channelId);
      setNlmStatus(status);
      setStep("done");
    } catch (err) {
      setError(err instanceof Error ? err.message : "綁定失敗");
    } finally {
      setLoading(false);
    }
  };

  const inputStyle = { width: "100%", padding: 10, fontSize: 14, marginBottom: 12, boxSizing: "border-box" as const };
  const btnStyle = { width: "100%", padding: 10, fontSize: 16, cursor: "pointer" };

  return (
    <div style={{ maxWidth: 500, margin: "40px auto", padding: 24 }}>
      <h1 style={{ fontSize: 24, marginBottom: 24 }}>Channel 設定</h1>

      {step === "channel" && (
        <form onSubmit={handleChannelSubmit}>
          <label>LINE Channel ID</label>
          <input style={inputStyle} value={channelId} onChange={(e) => setChannelId(e.target.value)} required />
          <label>Channel Secret</label>
          <input style={inputStyle} type="password" value={channelSecret} onChange={(e) => setChannelSecret(e.target.value)} required />
          <label>Channel Access Token</label>
          <input style={inputStyle} type="password" value={accessToken} onChange={(e) => setAccessToken(e.target.value)} required />
          {error && <p style={{ color: "red" }}>{error}</p>}
          <button type="submit" disabled={loading} style={btnStyle}>
            {loading ? "建立中..." : "建立 Channel"}
          </button>
        </form>
      )}

      {step === "nlm" && (
        <>
          <div style={{ background: "#f0f9ff", padding: 16, borderRadius: 8, marginBottom: 20 }}>
            <p style={{ margin: 0, fontWeight: "bold" }}>✅ Channel 已建立</p>
            <p style={{ margin: "8px 0 0", fontSize: 14 }}>
              請將以下 Webhook URL 貼到 LINE Developers Console：
            </p>
            <code style={{ display: "block", marginTop: 8, padding: 8, background: "#e2e8f0", borderRadius: 4, wordBreak: "break-all" }}>
              {webhookUrl}
            </code>
          </div>

          <h2 style={{ fontSize: 18, marginBottom: 12 }}>綁定 NotebookLM</h2>

          <div style={{ display: "flex", gap: 8, marginBottom: 16 }}>
            <button
              onClick={() => setBindMode("auto")}
              style={{ ...btnStyle, width: "auto", flex: 1, background: bindMode === "auto" ? "#3b82f6" : "#e5e7eb", color: bindMode === "auto" ? "#fff" : "#333", border: "none", borderRadius: 6 }}
            >
              🔄 自動讀取
            </button>
            <button
              onClick={() => setBindMode("manual")}
              style={{ ...btnStyle, width: "auto", flex: 1, background: bindMode === "manual" ? "#3b82f6" : "#e5e7eb", color: bindMode === "manual" ? "#fff" : "#333", border: "none", borderRadius: 6 }}
            >
              📋 手動貼上
            </button>
          </div>

          {bindMode === "auto" && (
            <div>
              <div style={{ background: "#fffbeb", padding: 12, borderRadius: 8, marginBottom: 12, fontSize: 14 }}>
                <p style={{ margin: 0 }}>📌 請先在伺服器 terminal 執行：</p>
                <code style={{ display: "block", marginTop: 8, padding: 8, background: "#fef3c7", borderRadius: 4 }}>
                  notebooklm login
                </code>
                <p style={{ margin: "8px 0 0" }}>完成 Google 登入後，點擊下方按鈕自動綁定。</p>
              </div>
              {error && <p style={{ color: "red", marginBottom: 12 }}>{error}</p>}
              <button onClick={handleAutoBind} disabled={loading} style={btnStyle}>
                {loading ? "綁定中..." : "讀取登入狀態並綁定"}
              </button>
            </div>
          )}

          {bindMode === "manual" && (
            <form onSubmit={handleManualBind}>
              <p style={{ fontSize: 14, color: "#666", marginBottom: 12 }}>
                上傳 <code>storage_state.json</code> 檔案，或將內容貼到下方：
              </p>
              <input
                type="file"
                accept=".json"
                style={{ marginBottom: 12 }}
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (!file) return;
                  const reader = new FileReader();
                  reader.onload = () => setNlmJson(reader.result as string);
                  reader.readAsText(file);
                }}
              />
              <textarea
                style={{ ...inputStyle, height: 120, fontFamily: "monospace" }}
                value={nlmJson}
                onChange={(e) => setNlmJson(e.target.value)}
                placeholder='{"cookies": [...], ...}'
                required
              />
              {error && <p style={{ color: "red" }}>{error}</p>}
              <button type="submit" disabled={loading} style={btnStyle}>
                {loading ? "綁定中..." : "綁定 NotebookLM"}
              </button>
            </form>
          )}
        </>
      )}

      {step === "done" && (
        <div style={{ textAlign: "center" }}>
          <div style={{ fontSize: 48, marginBottom: 16 }}>🎉</div>
          <h2>設定完成！</h2>
          <p>NotebookLM 已綁定</p>
          <p style={{ fontSize: 14, color: "#666" }}>
            Notebook ID: <code>{nlmStatus.notebook_id}</code>
          </p>
          <div style={{ background: "#f0fdf4", padding: 16, borderRadius: 8, marginTop: 16 }}>
            <p style={{ margin: 0 }}>Webhook URL:</p>
            <code style={{ wordBreak: "break-all" }}>{webhookUrl}</code>
          </div>
          <p style={{ marginTop: 16, fontSize: 14, color: "#666" }}>
            使用者現在可以在你的 LINE 官方帳號上直接提問了！
          </p>
        </div>
      )}
    </div>
  );
}
