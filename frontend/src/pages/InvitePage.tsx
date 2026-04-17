import { useState, FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { verifyInvite } from "../lib/api";

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
      const { token } = await verifyInvite(code);
      sessionStorage.setItem("token", token);
      navigate("/setup");
    } catch (err) {
      setError(err instanceof Error ? err.message : "驗證失敗");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: 400, margin: "80px auto", padding: 24 }}>
      <h1 style={{ fontSize: 24, marginBottom: 8 }}>NotebookLM LINE Bot</h1>
      <p style={{ color: "#666", marginBottom: 24 }}>請輸入邀請碼開始設定</p>
      <form onSubmit={handleSubmit}>
        <input
          type="text"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="邀請碼"
          required
          style={{ width: "100%", padding: 10, fontSize: 16, marginBottom: 12, boxSizing: "border-box" }}
        />
        {error && <p style={{ color: "red", marginBottom: 12 }}>{error}</p>}
        <button
          type="submit"
          disabled={loading}
          style={{ width: "100%", padding: 10, fontSize: 16, cursor: "pointer" }}
        >
          {loading ? "驗證中..." : "驗證"}
        </button>
      </form>
    </div>
  );
}
