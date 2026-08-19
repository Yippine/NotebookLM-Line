import { FormEvent, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  ApiError,
  bindNotebook,
  createChannel,
  formatApiError,
  getChannel,
  getNotebookBinding,
  getPublicCourseAccount,
  isSessionExpired,
  NotebookBinding,
  NotebookBindingStatus,
  PublicCourseAccount,
  recheckNotebookBinding,
  unbindNotebook,
} from "../lib/api";

const STEPS = ["LINE Channel", "NotebookLM", "完成"] as const;

const EMPTY_BINDING: NotebookBinding = {
  status: "unbound",
  notebook_id: null,
  notebook_title: null,
  last_access_checked_at: null,
};

type BusyAction = "channel" | "bind" | "recheck" | "unbind" | null;

const STATUS_CONTENT: Record<
  NotebookBindingStatus,
  { label: string; title: string; description: string; className: string }
> = {
  unbound: {
    label: "尚未綁定",
    title: "等待分享 Notebook",
    description: "依照下方步驟分享你的 Notebook，再貼上網址進行測試。",
    className: "border-warm-200 bg-white/60 text-warm-700",
  },
  checking: {
    label: "檢查中",
    title: "正在確認讀取與問答功能",
    description: "這可能需要一些時間，請保持頁面開啟且不要重複送出。",
    className: "border-amber-200 bg-amber-50 text-amber-800",
  },
  bound: {
    label: "連線正常",
    title: "NotebookLM 已成功綁定",
    description: "你的 LINE 官方帳號現在會使用這本 Notebook 回答問題。",
    className: "border-emerald-200 bg-emerald-50 text-emerald-800",
  },
  access_revoked: {
    label: "無法存取",
    title: "Notebook 分享可能已取消",
    description: "請在 NotebookLM 重新分享給課程帳號並設為檢視者，再按「重新檢查」。",
    className: "border-red-200 bg-red-50 text-red-800",
  },
  course_account_unavailable: {
    label: "課程帳號暫不可用",
    title: "需要管理者處理課程帳號",
    description: "你的 Notebook 與 LINE 設定不會被刪除，請聯繫管理者完成重新驗證後再檢查。",
    className: "border-amber-200 bg-amber-50 text-amber-800",
  },
  error: {
    label: "暫時無法確認",
    title: "NotebookLM 連線發生問題",
    description: "請稍後按「重新檢查」；如果持續失敗，請將查詢編號提供給管理者。",
    className: "border-red-200 bg-red-50 text-red-800",
  },
};

function StepIndicator({ current }: { current: number }) {
  return (
    <div className="flex items-center justify-center gap-0 mb-8" aria-label={`設定進度：第 ${current + 1} 步`}>
      {STEPS.map((label, index) => (
        <div key={label} className="flex items-center">
          <div className="flex flex-col items-center">
            <div
              className={`w-9 h-9 rounded-full flex items-center justify-center text-sm font-bold transition-all duration-300 ${
                index < current
                  ? "bg-copper text-white shadow-mid"
                  : index === current
                    ? "bg-warm-800 text-white shadow-deep ring-4 ring-copper/20"
                    : "bg-surface-inset text-warm-400 shadow-inset"
              }`}
            >
              {index < current ? "✓" : index + 1}
            </div>
            <span className={`text-xs mt-1.5 font-medium ${index <= current ? "text-warm-700" : "text-warm-400"}`}>
              {label}
            </span>
          </div>
          {index < STEPS.length - 1 && (
            <div className={`w-16 h-0.5 mx-2 mb-5 rounded ${index < current ? "bg-copper" : "bg-warm-300"}`} />
          )}
        </div>
      ))}
    </div>
  );
}

function Spinner({ dark = false }: { dark?: boolean }) {
  return (
    <span
      className={`w-4 h-4 border-2 rounded-full animate-spin ${
        dark ? "border-warm-300 border-t-warm-700" : "border-white/30 border-t-white"
      }`}
      aria-hidden="true"
    />
  );
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "尚未檢查";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "尚未檢查" : date.toLocaleString("zh-TW");
}

function isBindingStatus(value: string | undefined): value is NotebookBindingStatus {
  return !!value && value in STATUS_CONTENT;
}

function validateNotebookUrl(value: string): string | null {
  try {
    const parsed = new URL(value.trim());
    const allowedHosts = new Set([
      "notebooklm.google.com",
      "notebooklm.google",
      "notebook.google.com",
    ]);
    if (parsed.protocol !== "https:" || !allowedHosts.has(parsed.hostname.toLowerCase())) {
      return "請貼上 NotebookLM 官方 HTTPS 網址。";
    }
    if (!parsed.pathname.startsWith("/notebook/") || parsed.pathname.split("/").filter(Boolean).length < 2) {
      return "網址中找不到 Notebook 識別碼，請從已開啟的 Notebook 複製完整網址。";
    }
    return null;
  } catch {
    return "Notebook 網址格式不正確，請重新複製完整網址。";
  }
}

export default function SetupPage() {
  const navigate = useNavigate();
  const token = sessionStorage.getItem("token") || "";

  const [channelId, setChannelId] = useState("");
  const [channelSecret, setChannelSecret] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [webhookUrl, setWebhookUrl] = useState("");
  const [step, setStep] = useState(0);
  const [courseAccount, setCourseAccount] = useState<PublicCourseAccount>({
    email: null,
    health_status: "unconfigured",
  });
  const [binding, setBinding] = useState<NotebookBinding>(EMPTY_BINDING);
  const [notebookUrl, setNotebookUrl] = useState("");
  const [rebindMode, setRebindMode] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<BusyAction>(null);
  const [copied, setCopied] = useState(false);

  const expireSetupSession = () => {
    sessionStorage.removeItem("token");
    setError("工作階段已過期，請重新輸入邀請碼。");
    navigate("/", { replace: true });
  };

  const showProtectedError = (cause: unknown, fallback: string) => {
    if (isSessionExpired(cause)) {
      expireSetupSession();
      return;
    }
    setError(formatApiError(cause, fallback));
  };

  useEffect(() => {
    document.title = "AI NoteBook 設定";
    if (!token) {
      navigate("/", { replace: true });
      return;
    }

    let cancelled = false;
    const restore = async () => {
      const savedChannelId = sessionStorage.getItem("channel_id");
      if (!savedChannelId) return;
      setChannelId(savedChannelId);
      setBusy("channel");
      try {
        const channel = await getChannel(token, savedChannelId);
        if (cancelled) return;
        setWebhookUrl(channel.webhook_url);
        setStep(channel.binding_status === "bound" || channel.nlm_bound ? 2 : 1);

        const [accountResult, bindingResult] = await Promise.allSettled([
          getPublicCourseAccount(token),
          getNotebookBinding(token, savedChannelId),
        ]);
        if (cancelled) return;

        if (accountResult.status === "fulfilled") setCourseAccount(accountResult.value);
        else if (isSessionExpired(accountResult.reason)) return expireSetupSession();

        if (bindingResult.status === "fulfilled") {
          setBinding(bindingResult.value);
          setStep(bindingResult.value.status === "bound" ? 2 : 1);
        } else if (isSessionExpired(bindingResult.reason)) {
          return expireSetupSession();
        } else {
          const fallbackStatus = channel.binding_status || (channel.nlm_bound ? "bound" : "unbound");
          setBinding({
            status: fallbackStatus,
            notebook_id: channel.notebook_id,
            notebook_title: channel.notebook_display_name || null,
            last_access_checked_at: channel.last_access_checked_at || null,
          });
        }
      } catch (cause) {
        if (cancelled) return;
        if (isSessionExpired(cause)) expireSetupSession();
        else setStep(0);
      } finally {
        if (!cancelled) setBusy(null);
      }
    };

    void restore();
    return () => {
      cancelled = true;
    };
  }, [navigate, token]);

  const refreshCourseAccount = async () => {
    try {
      setCourseAccount(await getPublicCourseAccount(token));
    } catch (cause) {
      showProtectedError(cause, "暫時無法讀取課程帳號資訊。");
    }
  };

  const handleChannelSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setError("");
    setBusy("channel");
    try {
      const response = await createChannel(token, {
        channel_id: channelId.trim(),
        channel_secret: channelSecret.trim(),
        channel_access_token: accessToken.trim(),
      });
      setWebhookUrl(response.webhook_url);
      setChannelId(response.channel_id);
      sessionStorage.setItem("channel_id", response.channel_id);
      // LINE credentials are intentionally never persisted or shown again.
      setChannelSecret("");
      setAccessToken("");
      setStep(1);
      setBinding(EMPTY_BINDING);
      await refreshCourseAccount();
    } catch (cause) {
      showProtectedError(cause, "建立 LINE Channel 失敗，請確認資料後再試。");
    } finally {
      setBusy(null);
    }
  };

  const handleBind = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setError("");
    const urlError = validateNotebookUrl(notebookUrl);
    if (urlError) {
      setError(urlError);
      return;
    }

    const previous = binding;
    setBusy("bind");
    setBinding({ ...binding, status: "checking" });
    try {
      const result = await bindNotebook(token, channelId, notebookUrl.trim());
      setBinding(result);
      setNotebookUrl("");
      setRebindMode(false);
      if (result.status === "bound") setStep(2);
    } catch (cause) {
      if (cause instanceof ApiError && isBindingStatus(cause.resourceStatus)) {
        setBinding({ ...previous, status: cause.resourceStatus, request_id: cause.requestId });
      } else {
        setBinding(previous.status === "bound" ? previous : { ...previous, status: "error" });
      }
      showProtectedError(cause, "NotebookLM 綁定失敗，請稍後再試。");
    } finally {
      setBusy(null);
    }
  };

  const handleRecheck = async () => {
    if (busy) return;
    setError("");
    const previous = binding;
    setBusy("recheck");
    setBinding({ ...binding, status: "checking" });
    try {
      const result = await recheckNotebookBinding(token, channelId);
      setBinding(result);
      setStep(result.status === "bound" ? 2 : 1);
    } catch (cause) {
      if (cause instanceof ApiError && isBindingStatus(cause.resourceStatus)) {
        setBinding({ ...previous, status: cause.resourceStatus, request_id: cause.requestId });
      } else {
        setBinding({ ...previous, status: previous.notebook_id ? "error" : "unbound" });
      }
      showProtectedError(cause, "重新檢查失敗，請稍後再試。");
    } finally {
      setBusy(null);
    }
  };

  const handleUnbind = async () => {
    if (busy || !confirm("確定要解除這個 LINE Channel 的 Notebook 綁定嗎？")) return;
    setError("");
    setBusy("unbind");
    try {
      const result = await unbindNotebook(token, channelId);
      setBinding(result || EMPTY_BINDING);
      setNotebookUrl("");
      setRebindMode(false);
      setStep(1);
    } catch (cause) {
      showProtectedError(cause, "解除綁定失敗，請稍後再試。");
    } finally {
      setBusy(null);
    }
  };

  const copyWebhook = async () => {
    if (!webhookUrl) return;
    try {
      await navigator.clipboard.writeText(webhookUrl);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2000);
    } catch {
      setError("無法自動複製，請手動選取 Webhook URL。");
    }
  };

  const accountReady = courseAccount.health_status === "healthy" && !!courseAccount.email;
  const effectiveBindingStatus: NotebookBindingStatus =
    binding.status === "bound" && !accountReady
      ? "course_account_unavailable"
      : binding.status;
  const statusContent = STATUS_CONTENT[effectiveBindingStatus];

  const ErrorBanner = () =>
    error ? (
      <div role="alert" className="flex items-start gap-2 px-4 py-3 rounded-tactile bg-red-50 border border-red-200 text-red-700 text-sm">
        <span aria-hidden="true">⚠</span>
        <span>{error}</span>
      </div>
    ) : null;

  const WebhookCard = () => (
    <div className="card-base">
      <div className="flex items-center gap-2 mb-2">
        <span className="badge-success">✓ Channel 已建立</span>
      </div>
      <p className="text-sm text-warm-600 mb-2">請將以下 Webhook URL 貼到 LINE Developers Console：</p>
      <div className="flex items-center gap-2">
        <div className="flex-1 bg-surface-inset shadow-inset rounded-lg px-3 py-2.5 font-mono text-sm text-warm-700 break-all">
          {webhookUrl}
        </div>
        <button onClick={() => void copyWebhook()} type="button" className="shrink-0 btn-ghost !p-2.5" title="複製 Webhook URL">
          {copied ? "✓" : "複製"}
        </button>
      </div>
    </div>
  );

  const BindingStatusCard = () => (
    <div className={`rounded-tactile border p-4 text-left ${statusContent.className}`} aria-live="polite">
      <div className="flex items-center justify-between gap-3 mb-1">
        <h3 className="font-semibold">{statusContent.title}</h3>
        <span className="badge bg-white/60 border border-current/20 shrink-0">
          {effectiveBindingStatus === "checking" && <Spinner dark />} {statusContent.label}
        </span>
      </div>
      <p className="text-sm opacity-90">{statusContent.description}</p>
      {binding.notebook_title && <p className="text-sm font-semibold mt-3">目前 Notebook：{binding.notebook_title}</p>}
      {binding.notebook_id && !binding.notebook_title && (
        <p className="text-xs font-mono mt-3 break-all">Notebook ID：{binding.notebook_id}</p>
      )}
      {binding.notebook_id && (
        <p className="text-xs opacity-75 mt-1">最後檢查：{formatTime(binding.last_access_checked_at)}</p>
      )}
      {binding.request_id && <p className="text-xs opacity-75 mt-1">查詢編號：{binding.request_id}</p>}
    </div>
  );

  const SharingGuide = () => (
    <div className="card-base space-y-4 text-left">
      <div>
        <h2 className="font-display text-lg font-bold text-warm-800">分享自己的 Notebook</h2>
        <p className="text-sm text-warm-500 mt-1">不需要安裝套件，也不需要提供 Google 密碼或 Cookie。</p>
      </div>
      <ol className="space-y-3 text-sm text-warm-700">
        <li className="flex gap-3"><span className="badge bg-copper/10 text-copper">1</span><span>開啟你自己建立的 NotebookLM Notebook，點選右上角「分享」。</span></li>
        <li className="flex gap-3">
          <span className="badge bg-copper/10 text-copper">2</span>
          <span>
            私人分享給課程帳號，權限選擇「檢視者（Viewer）」：
            <strong className="block mt-1 font-mono text-warm-800 break-all">{courseAccount.email || "管理者尚未設定課程 Gmail"}</strong>
          </span>
        </li>
        <li className="flex gap-3"><span className="badge bg-copper/10 text-copper">3</span><span>回到該 Notebook，從瀏覽器網址列複製完整網址並貼到下方。</span></li>
      </ol>
      <div className="rounded-lg bg-amber-50 border border-amber-200 px-4 py-3 text-sm text-amber-900">
        <strong>資料可見性：</strong>課程帳號會讀取你分享的 Notebook、來源內容與產生的回答，以便 LINE Bot 回答。請只分享課程需要的資料。
      </div>
      {!accountReady && (
        <div className="rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700">
          課程 NotebookLM 帳號目前尚未可用，請聯繫管理者；你可以稍後回來完成綁定。
        </div>
      )}
    </div>
  );

  const CancelSharingGuide = () => (
    <details className="card-base text-left">
      <summary className="font-semibold text-warm-700 cursor-pointer">如何停止課程帳號讀取我的 Notebook？</summary>
      <ol className="mt-3 ml-5 list-decimal space-y-1.5 text-sm text-warm-600">
        <li>先在本頁按「解除綁定」，停止 LINE Bot 使用這本 Notebook。</li>
        <li>回到 NotebookLM 的「分享」設定。</li>
        <li>移除課程帳號 <span className="font-mono break-all">{courseAccount.email || ""}</span> 的存取權。</li>
      </ol>
      <p className="text-xs text-warm-500 mt-3">解除系統綁定不會自動變更 Google 的分享設定，兩個步驟都完成才會停止存取。</p>
    </details>
  );

  return (
    <div className="min-h-screen flex items-center justify-center p-6">
      <div className="card-raised w-full max-w-2xl">
        <h1 className="font-display text-2xl font-bold text-warm-800 text-center mb-2">AI NoteBook 設定</h1>
        <p className="text-warm-500 text-sm text-center mb-6">將 LINE 官方帳號與你自己的 NotebookLM 知識庫連線</p>

        <StepIndicator current={step} />

        {step === 0 && (
          <form onSubmit={handleChannelSubmit} className="space-y-4">
            <div>
              <label htmlFor="line-channel-id" className="block text-xs font-semibold text-warm-600 uppercase tracking-wider mb-2">LINE Channel ID</label>
              <input id="line-channel-id" className="input-tactile" value={channelId} onChange={(event) => setChannelId(event.target.value)} required autoComplete="off" />
            </div>
            <div>
              <label htmlFor="line-channel-secret" className="block text-xs font-semibold text-warm-600 uppercase tracking-wider mb-2">Channel Secret</label>
              <input id="line-channel-secret" className="input-tactile" type="password" value={channelSecret} onChange={(event) => setChannelSecret(event.target.value)} required autoComplete="new-password" />
            </div>
            <div>
              <label htmlFor="line-access-token" className="block text-xs font-semibold text-warm-600 uppercase tracking-wider mb-2">Channel Access Token</label>
              <input id="line-access-token" className="input-tactile" type="password" value={accessToken} onChange={(event) => setAccessToken(event.target.value)} required autoComplete="new-password" />
            </div>
            <p className="text-xs text-warm-500">系統會自動移除三個欄位首尾的空白與換行；Secret 與 Access Token 送出後不會在頁面顯示或保存於瀏覽器。</p>
            <ErrorBanner />
            <button type="submit" disabled={busy !== null} className="btn-primary">
              {busy === "channel" ? <span className="flex items-center justify-center gap-2"><Spinner />建立中</span> : "建立 Channel"}
            </button>
          </form>
        )}

        {step === 1 && (
          <div className="space-y-5">
            <WebhookCard />
            <BindingStatusCard />
            <SharingGuide />
            <form onSubmit={handleBind} className="space-y-3">
              <div>
                <label htmlFor="notebook-url" className="block text-xs font-semibold text-warm-600 uppercase tracking-wider mb-2">你的 NotebookLM 網址</label>
                <input
                  id="notebook-url"
                  type="url"
                  inputMode="url"
                  className="input-tactile"
                  value={notebookUrl}
                  onChange={(event) => setNotebookUrl(event.target.value)}
                  placeholder="https://notebook.google.com/notebook/..."
                  required
                  autoComplete="off"
                  disabled={busy !== null}
                />
              </div>
              <p className="text-xs text-warm-500">按下後會使用課程帳號產生一次最小測試查詢，確認檢視與回答功能正常後才會保存綁定。</p>
              <ErrorBanner />
              <button type="submit" disabled={busy !== null || !notebookUrl.trim() || !accountReady} className="btn-primary">
                {busy === "bind" ? <span className="flex items-center justify-center gap-2"><Spinner />正在測試，請勿重複送出</span> : rebindMode ? "測試並更新綁定" : "測試並綁定"}
              </button>
            </form>

            {binding.notebook_id && binding.status !== "unbound" && (
              <div className="flex flex-wrap gap-3">
                <button type="button" onClick={() => void handleRecheck()} disabled={busy !== null} className="btn-copper flex-1">{busy === "recheck" ? "檢查中..." : "重新檢查"}</button>
                <button type="button" onClick={() => void handleUnbind()} disabled={busy !== null} className="btn-ghost flex-1">{busy === "unbind" ? "解除中..." : "解除綁定"}</button>
              </div>
            )}
            <CancelSharingGuide />
            <button type="button" onClick={() => { setStep(0); setError(""); }} className="btn-ghost w-full text-sm">← 返回修改 LINE Channel 設定</button>
          </div>
        )}

        {step === 2 && (
          <div className="text-center space-y-5">
            <div className="mx-auto w-20 h-20 rounded-full bg-gradient-to-br from-copper-light to-copper shadow-deep flex items-center justify-center"><span className="text-3xl">{effectiveBindingStatus === "course_account_unavailable" ? "⚠️" : "🎉"}</span></div>
            <div>
              <h2 className="font-display text-xl font-bold text-warm-800 mb-1">{effectiveBindingStatus === "course_account_unavailable" ? "Notebook 綁定已保留" : "設定完成！"}</h2>
              <p className="text-warm-500 text-sm">{effectiveBindingStatus === "course_account_unavailable" ? "課程帳號暫時無法連線，請聯繫管理者重新驗證；不需要重新綁定 Notebook。" : "使用者現在可以在你的 LINE 官方帳號上直接提問。"}</p>
            </div>
            <BindingStatusCard />
            <WebhookCard />
            <ErrorBanner />
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
              <button type="button" onClick={() => void handleRecheck()} disabled={busy !== null} className="btn-copper">{busy === "recheck" ? "檢查中..." : "重新檢查"}</button>
              <button type="button" onClick={() => { setRebindMode(true); setNotebookUrl(""); setError(""); setStep(1); }} disabled={busy !== null} className="btn-ghost">重新綁定</button>
              <button type="button" onClick={() => void handleUnbind()} disabled={busy !== null} className="btn-ghost text-red-600 border-red-200">{busy === "unbind" ? "解除中..." : "解除綁定"}</button>
            </div>
            <CancelSharingGuide />
            <button type="button" onClick={() => { setStep(0); setError(""); }} className="btn-ghost w-full text-sm">← 修改 LINE 設定（需重新輸入憑證）</button>
          </div>
        )}
      </div>
    </div>
  );
}
