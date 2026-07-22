import { useState, FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, formatApiError, verifyInvite } from "../lib/api";

export default function InvitePage() {
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const res = await verifyInvite(code);
      sessionStorage.setItem("token", res.token);
      if (res.channel_id) {
        sessionStorage.setItem("channel_id", res.channel_id);
      } else {
        sessionStorage.removeItem("channel_id");
      }
      navigate("/setup");
    } catch (err) {
      setError(
        err instanceof ApiError && err.statusCode === 400
          ? "邀請碼無效、已過期，或課程已結束。"
          : formatApiError(err, "驗證失敗，請稍後再試。"),
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center p-6">
      <div className="card-raised w-full max-w-sm text-center">
        {/* Copper accent circle */}
        <div className="mx-auto mb-6 w-16 h-16 rounded-full bg-gradient-to-br from-copper-light to-copper shadow-mid flex items-center justify-center">
          <svg className="w-8 h-8 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 6.042A8.967 8.967 0 006 3.75c-1.052 0-2.062.18-3 .512v14.25A8.987 8.987 0 016 18c2.305 0 4.408.867 6 2.292m0-14.25a8.966 8.966 0 016-2.292c1.052 0 2.062.18 3 .512v14.25A8.987 8.987 0 0018 18a8.967 8.967 0 00-6 2.292m0-14.25v14.25" />
          </svg>
        </div>

        <h1 className="font-display text-2xl font-bold text-warm-800 mb-1">
          AI-Notebook
        </h1>
        <p className="text-warm-500 text-sm mb-8">讓你的 LINE 擁有 AI 知識大腦</p>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="text-left">
            <label className="block text-xs font-semibold text-warm-600 uppercase tracking-wider mb-2">
              邀請碼
            </label>
            <input
              type="text"
              value={code}
              onChange={(e) => setCode(e.target.value)}
              placeholder="請輸入邀請碼"
              required
              className="input-tactile text-center tracking-widest text-lg"
            />
          </div>

          {error && (
            <div className="flex items-center gap-2 px-4 py-2.5 rounded-tactile bg-red-50 border border-red-200 text-red-700 text-sm">
              <span>⚠</span>
              <span>{error}</span>
            </div>
          )}

          <button type="submit" disabled={loading} className="btn-primary">
            {loading ? (
              <span className="flex items-center justify-center gap-2">
                <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
                驗證中
              </span>
            ) : "開始設定"}
          </button>
        </form>

        <p className="mt-6 text-xs text-warm-400">
          請向講師索取邀請碼
        </p>
      </div>
    </div>
  );
}
