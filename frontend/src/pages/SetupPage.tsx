import { useState, FormEvent, useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { createChannel, nlmLogin, getNlmStatus, getNotebooks, selectNotebook, getChannel } from "../lib/api";

const STEPS = ["LINE Channel", "NotebookLM", "完成"] as const;

type Notebook = { id: string; title: string };

function StepIndicator({ current }: { current: number }) {
  return (
    <div className="flex items-center justify-center gap-0 mb-8">
      {STEPS.map((label, i) => (
        <div key={label} className="flex items-center">
          <div className="flex flex-col items-center">
            <div
              className={`w-9 h-9 rounded-full flex items-center justify-center text-sm font-bold transition-all duration-300 ${
                i < current
                  ? "bg-copper text-white shadow-mid"
                  : i === current
                  ? "bg-warm-800 text-white shadow-deep ring-4 ring-copper/20"
                  : "bg-surface-inset text-warm-400 shadow-inset"
              }`}
            >
              {i < current ? "✓" : i + 1}
            </div>
            <span className={`text-xs mt-1.5 font-medium ${
              i <= current ? "text-warm-700" : "text-warm-400"
            }`}>
              {label}
            </span>
          </div>
          {i < STEPS.length - 1 && (
            <div className={`w-16 h-0.5 mx-2 mb-5 rounded transition-colors duration-300 ${
              i < current ? "bg-copper" : "bg-warm-300"
            }`} />
          )}
        </div>
      ))}
    </div>
  );
}

export default function SetupPage() {
  const navigate = useNavigate();
  const token = sessionStorage.getItem("token") || "";

  const [channelId, setChannelId] = useState("");
  const [channelSecret, setChannelSecret] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [step, setStep] = useState(0);
  const [nlmJson, setNlmJson] = useState("");
  const [nlmStatus, setNlmStatus] = useState<{ bound: boolean; notebook_id: string | null }>({ bound: false, notebook_id: null });
  const [notebooks, setNotebooks] = useState<Notebook[]>([]);
  const [selectedNotebook, setSelectedNotebook] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!token) { navigate("/"); return; }

    // Resume from existing binding state
    const savedChannelId = sessionStorage.getItem("channel_id");
    if (savedChannelId) {
      setChannelId(savedChannelId);
      // Fetch channel info to get webhook URL, then check NLM status
      getChannel(token, savedChannelId).then((ch) => {
        setWebhookUrl(ch.webhook_url);
        setNlmStatus({ bound: ch.nlm_bound, notebook_id: ch.notebook_id });
        if (ch.nlm_bound) {
          getNotebooks(savedChannelId).then((res) => {
            setNotebooks(res.notebooks);
            if (ch.notebook_id) setSelectedNotebook(ch.notebook_id);
            else if (res.notebooks.length > 0) setSelectedNotebook(res.notebooks[0].id);
          }).catch(() => {});
          setStep(2);
        } else {
          setStep(1);
        }
      }).catch(() => {
        setStep(0);
      });
    }
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
      setStep(1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "建立失敗");
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
      const res = await nlmLogin(channelId, parsed);
      const nbs = (res as unknown as { notebooks: Notebook[] }).notebooks || [];
      setNotebooks(nbs);
      if (nbs.length > 0) setSelectedNotebook(nbs[0].id);
      const status = await getNlmStatus(channelId);
      setNlmStatus(status);
      setStep(2);
    } catch (err) {
      setError(err instanceof Error ? err.message : "綁定失敗");
    } finally {
      setLoading(false);
    }
  };

  const ErrorBanner = () =>
    error ? (
      <div className="flex items-center gap-2 px-4 py-2.5 rounded-tactile bg-red-50 border border-red-200 text-red-700 text-sm mb-4">
        <span>⚠</span>
        <span>{error}</span>
      </div>
    ) : null;

  const Spinner = () => (
    <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
  );

  return (
    <div className="min-h-screen flex items-center justify-center p-6">
      <div className="card-raised w-full max-w-lg">
        <h1 className="font-display text-2xl font-bold text-warm-800 text-center mb-2">
          Channel 設定
        </h1>
        <p className="text-warm-500 text-sm text-center mb-6">設定你的 LINE 官方帳號與 NotebookLM</p>

        <StepIndicator current={step} />

        {/* Step 1: LINE Channel */}
        {step === 0 && (
          <form onSubmit={handleChannelSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-semibold text-warm-600 uppercase tracking-wider mb-2">
                LINE Channel ID
              </label>
              <input className="input-tactile" value={channelId} onChange={(e) => setChannelId(e.target.value)} required />
            </div>
            <div>
              <label className="block text-xs font-semibold text-warm-600 uppercase tracking-wider mb-2">
                Channel Secret
              </label>
              <input className="input-tactile" type="password" value={channelSecret} onChange={(e) => setChannelSecret(e.target.value)} required />
            </div>
            <div>
              <label className="block text-xs font-semibold text-warm-600 uppercase tracking-wider mb-2">
                Channel Access Token
              </label>
              <input className="input-tactile" type="password" value={accessToken} onChange={(e) => setAccessToken(e.target.value)} required />
            </div>
            <ErrorBanner />
            <button type="submit" disabled={loading} className="btn-primary">
              {loading ? <span className="flex items-center justify-center gap-2"><Spinner />建立中</span> : "建立 Channel"}
            </button>
          </form>
        )}

        {/* Step 2: NotebookLM Bind */}
        {step === 1 && (
          <div className="space-y-5">
            {/* Webhook URL card */}
            <div className="card-base">
              <div className="flex items-center gap-2 mb-2">
                <span className="badge-success">✓ Channel 已建立</span>
              </div>
              <p className="text-sm text-warm-600 mb-2">
                請將以下 Webhook URL 貼到 LINE Developers Console：
              </p>
              <div className="flex items-center gap-2">
                <div className="flex-1 bg-surface-inset shadow-inset rounded-lg px-3 py-2.5 font-mono text-sm text-warm-700 break-all">
                  {webhookUrl}
                </div>
                <button
                  onClick={() => { navigator.clipboard.writeText(webhookUrl); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
                  className="shrink-0 p-2.5 rounded-lg bg-surface-inset shadow-soft hover:bg-surface-pressed transition-colors cursor-pointer"
                  title="複製"
                >
                  {copied ? (
                    <svg className="w-4 h-4 text-emerald-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" /></svg>
                  ) : (
                    <svg className="w-4 h-4 text-warm-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}><path strokeLinecap="round" strokeLinejoin="round" d="M15.666 3.888A2.25 2.25 0 0013.5 2.25h-3c-1.03 0-1.9.693-2.166 1.638m7.332 0c.055.194.084.4.084.612v0a.75.75 0 01-.75.75H9.75a.75.75 0 01-.75-.75v0c0-.212.03-.418.084-.612m7.332 0c.646.049 1.288.11 1.927.184 1.1.128 1.907 1.077 1.907 2.185V19.5a2.25 2.25 0 01-2.25 2.25H6.75A2.25 2.25 0 014.5 19.5V6.257c0-1.108.806-2.057 1.907-2.185a48.208 48.208 0 011.927-.184" /></svg>
                  )}
                </button>
              </div>
            </div>

            {/* NLM bind — manual only */}
            <form onSubmit={handleManualBind} className="space-y-4">
              <p className="text-sm text-warm-600">
                上傳 <code className="text-copper font-mono text-xs bg-surface-inset px-1.5 py-0.5 rounded">storage_state.json</code> 或將內容貼到下方：
              </p>
              <input
                type="file"
                accept=".json"
                className="block w-full text-sm text-warm-500 file:mr-3 file:py-2 file:px-4 file:rounded-tactile file:border-0 file:text-sm file:font-medium file:bg-surface-inset file:text-warm-700 file:shadow-soft file:cursor-pointer hover:file:bg-surface-pressed"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (!file) return;
                  const reader = new FileReader();
                  reader.onload = () => setNlmJson(reader.result as string);
                  reader.readAsText(file);
                }}
              />
              <textarea
                className="input-tactile h-28 font-mono text-sm resize-none"
                value={nlmJson}
                onChange={(e) => setNlmJson(e.target.value)}
                placeholder='{"cookies": [...], ...}'
                required
              />
              <ErrorBanner />
              <button type="submit" disabled={loading} className="btn-primary">
                {loading ? <span className="flex items-center justify-center gap-2"><Spinner />綁定中</span> : "綁定 NotebookLM"}
              </button>
            </form>
            <button onClick={() => { setStep(0); setError(""); }} className="btn-ghost w-full mt-2 text-sm">
              ← 返回修改 LINE Channel 設定
            </button>
          </div>
        )}

        {/* Step 3: Done — select notebook */}
        {step === 2 && (
          <div className="text-center space-y-5">
            <div className="mx-auto w-20 h-20 rounded-full bg-gradient-to-br from-copper-light to-copper shadow-deep flex items-center justify-center">
              <span className="text-3xl">🎉</span>
            </div>
            <div>
              <h2 className="font-display text-xl font-bold text-warm-800 mb-1">設定完成！</h2>
              <p className="text-warm-500 text-sm">NotebookLM 已成功綁定，請選擇要使用的筆記本</p>
            </div>

            {/* Notebook selector */}
            <div className="card-base text-left">
              <label className="block text-xs font-semibold text-warm-600 uppercase tracking-wider mb-2">
                選擇筆記本
              </label>
              {notebooks.length > 0 ? (
                <>
                  <select
                    value={selectedNotebook}
                    onChange={(e) => setSelectedNotebook(e.target.value)}
                    className="input-tactile cursor-pointer"
                  >
                    {notebooks.map((nb) => (
                      <option key={nb.id} value={nb.id}>
                        {nb.title}
                      </option>
                    ))}
                  </select>
                  <button
                    onClick={async () => {
                      setError("");
                      setLoading(true);
                      try {
                        await selectNotebook(channelId, selectedNotebook);
                        setNlmStatus((prev) => ({ ...prev, notebook_id: selectedNotebook }));
                      } catch (err) {
                        setError(err instanceof Error ? err.message : "選擇失敗");
                      } finally {
                        setLoading(false);
                      }
                    }}
                    disabled={loading || selectedNotebook === nlmStatus.notebook_id}
                    className="btn-copper w-full mt-3"
                  >
                    {loading ? "儲存中..." : selectedNotebook === nlmStatus.notebook_id ? "✓ 已選擇" : "確認選擇"}
                  </button>
                </>
              ) : (
                <p className="text-sm text-warm-500">未找到任何筆記本，請先在 NotebookLM 建立筆記本。</p>
              )}
              {error && (
                <div className="flex items-center gap-2 mt-3 px-4 py-2.5 rounded-tactile bg-red-50 border border-red-200 text-red-700 text-sm">
                  <span>⚠</span><span>{error}</span>
                </div>
              )}
            </div>

            {/* Current selection display */}
            {nlmStatus.notebook_id && (
              <div className="card-base text-left">
                <p className="text-xs font-semibold text-warm-600 uppercase tracking-wider mb-1">目前使用的筆記本</p>
                <p className="text-sm text-warm-700 font-medium">
                  {notebooks.find((nb) => nb.id === nlmStatus.notebook_id)?.title || nlmStatus.notebook_id}
                </p>
              </div>
            )}

            <div className="card-base text-left">
              <p className="text-xs font-semibold text-warm-600 uppercase tracking-wider mb-1">Webhook URL</p>
              <p className="font-mono text-sm text-warm-700 break-all">{webhookUrl}</p>
            </div>
            <div className="bg-surface-inset shadow-inset rounded-tactile px-4 py-3">
              <p className="text-sm text-warm-600">
                ✨ 使用者現在可以在你的 LINE 官方帳號上直接提問了！
              </p>
            </div>
            <div className="flex gap-3 pt-2">
              <button onClick={() => { setStep(0); setError(""); }} className="btn-ghost flex-1 text-sm">
                ← 修改 LINE 設定
              </button>
              <button onClick={() => { setStep(1); setError(""); }} className="btn-ghost flex-1 text-sm">
                ← 重新綁定 NLM
              </button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
