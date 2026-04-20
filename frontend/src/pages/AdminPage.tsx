import { useState, useEffect, FormEvent } from "react";
import { adminLogin, getStudents, generateInviteCodes, deleteChannel } from "../lib/api";

type Student = {
  code: string;
  used: boolean;
  channel_id: string | null;
  nlm_bound: boolean;
  notebook_id: string | null;
  created_at: string;
};

export default function AdminPage() {
  const [password, setPassword] = useState("");
  const [authed, setAuthed] = useState(false);
  const [students, setStudents] = useState<Student[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [genCount, setGenCount] = useState(10);
  const [newCodes, setNewCodes] = useState<string[]>([]);

  const storedPw = () => sessionStorage.getItem("admin_pw") || "";

  const handleLogin = async (e: FormEvent) => {
    e.preventDefault();
    setError("");
    try {
      await adminLogin(password);
      sessionStorage.setItem("admin_pw", password);
      setAuthed(true);
      await refresh(password);
    } catch {
      setError("密碼錯誤");
    }
  };

  const refresh = async (pw?: string) => {
    setLoading(true);
    try {
      const data = await getStudents(pw || storedPw());
      setStudents(data);
    } catch {
      setError("載入失敗");
    } finally {
      setLoading(false);
    }
  };

  const handleGenerate = async () => {
    setError("");
    setNewCodes([]);
    try {
      const res = await generateInviteCodes(storedPw(), genCount);
      setNewCodes(res.codes);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "產生失敗");
    }
  };

  const handleDelete = async (channelId: string) => {
    if (!confirm(`確定要刪除 Channel ${channelId} 的綁定？`)) return;
    try {
      await deleteChannel(storedPw(), channelId);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "刪除失敗");
    }
  };

  useEffect(() => {
    const pw = sessionStorage.getItem("admin_pw");
    if (pw) {
      setPassword(pw);
      setAuthed(true);
      refresh(pw);
    }
  }, []);

  if (!authed) {
    return (
      <div className="min-h-screen flex items-center justify-center p-6">
        <div className="card-raised w-full max-w-sm text-center">
          <div className="mx-auto mb-6 w-14 h-14 rounded-full bg-warm-800 shadow-mid flex items-center justify-center">
            <svg className="w-7 h-7 text-white" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
            </svg>
          </div>
          <h1 className="font-display text-2xl font-bold text-warm-800 mb-6">講師管理後台</h1>
          <form onSubmit={handleLogin} className="space-y-4">
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="管理員密碼"
              required
              className="input-tactile text-center"
            />
            {error && (
              <div className="px-4 py-2.5 rounded-tactile bg-red-50 border border-red-200 text-red-700 text-sm">
                {error}
              </div>
            )}
            <button type="submit" className="btn-primary">登入</button>
          </form>
        </div>
      </div>
    );
  }

  const unused = students.filter((s) => !s.used);
  const active = students.filter((s) => s.used);

  return (
    <div className="min-h-screen p-6">
      <div className="max-w-4xl mx-auto space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <h1 className="font-display text-2xl font-bold text-warm-800">講師管理後台</h1>
          <button onClick={() => refresh()} disabled={loading} className="btn-ghost text-sm">
            {loading ? "載入中..." : "🔄 重新整理"}
          </button>
        </div>

        {error && (
          <div className="px-4 py-2.5 rounded-tactile bg-red-50 border border-red-200 text-red-700 text-sm">
            {error}
          </div>
        )}

        {/* Generate codes */}
        <div className="card-raised">
          <h2 className="font-display text-lg font-bold text-warm-800 mb-4">產生邀請碼</h2>
          <div className="flex items-center gap-3">
            <span className="text-sm text-warm-600">數量：</span>
            <input
              type="number"
              min={1}
              max={100}
              value={genCount}
              onChange={(e) => setGenCount(Number(e.target.value))}
              className="input-tactile w-24 !mb-0 text-center"
            />
            <button onClick={handleGenerate} className="btn-copper">產生</button>
          </div>
          {newCodes.length > 0 && (
            <div className="mt-4">
              <p className="text-sm text-warm-600 mb-2">
                <span className="badge-success">✓</span>
                <span className="ml-2">已產生 {newCodes.length} 組邀請碼</span>
              </p>
              <textarea
                readOnly
                value={newCodes.join("\n")}
                className="input-tactile h-24 font-mono text-sm resize-none cursor-text"
                onClick={(e) => (e.target as HTMLTextAreaElement).select()}
              />
            </div>
          )}
        </div>

        {/* Active students */}
        <div className="card-raised">
          <h2 className="font-display text-lg font-bold text-warm-800 mb-4">
            已使用的邀請碼
            <span className="ml-2 badge bg-copper/10 text-copper border border-copper/20">{active.length}</span>
          </h2>
          {active.length === 0 ? (
            <div className="text-center py-8">
              <p className="text-warm-400 text-sm">尚無學員使用邀請碼</p>
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b-2 border-warm-200 text-left">
                    <th className="pb-3 font-semibold text-warm-600 text-xs uppercase tracking-wider">邀請碼</th>
                    <th className="pb-3 font-semibold text-warm-600 text-xs uppercase tracking-wider">Channel ID</th>
                    <th className="pb-3 font-semibold text-warm-600 text-xs uppercase tracking-wider">NLM 狀態</th>
                    <th className="pb-3 font-semibold text-warm-600 text-xs uppercase tracking-wider">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {active.map((s) => (
                    <tr key={s.code} className="border-b border-warm-200/60 hover:bg-surface-inset/50 transition-colors">
                      <td className="py-3 font-mono text-warm-700">{s.code}</td>
                      <td className="py-3 font-mono text-warm-700">{s.channel_id || "—"}</td>
                      <td className="py-3">
                        {s.nlm_bound ? (
                          <span className="badge-success">✓ 已綁定</span>
                        ) : s.channel_id ? (
                          <span className="badge-warning">⏳ 未綁定</span>
                        ) : (
                          <span className="text-warm-400">—</span>
                        )}
                      </td>
                      <td className="py-3">
                        {s.channel_id && (
                          <button
                            onClick={() => handleDelete(s.channel_id!)}
                            className="text-xs px-3 py-1.5 rounded-lg border border-red-200 text-red-600 hover:bg-red-50 transition-colors cursor-pointer"
                          >
                            刪除
                          </button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        {/* Unused codes */}
        <div className="card-raised">
          <h2 className="font-display text-lg font-bold text-warm-800 mb-4">
            未使用的邀請碼
            <span className="ml-2 badge bg-warm-200 text-warm-600 border border-warm-300">{unused.length}</span>
          </h2>
          {unused.length === 0 ? (
            <p className="text-warm-400 text-sm text-center py-4">沒有未使用的邀請碼</p>
          ) : (
            <div className="flex flex-wrap gap-2">
              {unused.map((s) => (
                <span
                  key={s.code}
                  className="font-mono text-xs px-3 py-1.5 bg-surface-inset shadow-inset rounded-lg text-warm-600"
                >
                  {s.code}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
