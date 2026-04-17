import { Routes, Route, Navigate } from "react-router-dom";
import InvitePage from "./pages/InvitePage";
import SetupPage from "./pages/SetupPage";

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<InvitePage />} />
      <Route path="/setup" element={<SetupPage />} />
      <Route path="*" element={<Navigate to="/" />} />
    </Routes>
  );
}
