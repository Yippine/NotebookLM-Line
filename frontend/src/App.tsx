import { Routes, Route, Navigate } from "react-router-dom";
import InvitePage from "./pages/InvitePage";
import SetupPage from "./pages/SetupPage";
import AdminPage from "./pages/AdminPage";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<InvitePage />} />
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/admin" element={<AdminPage />} />
      <Route path="*" element={<Navigate to="/" />} />
    </Routes>
  );
}
