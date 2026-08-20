## Why（變更原因）

目前學員必須在自己的電腦安裝工具、執行 NotebookLM 登入並上傳 `storage_state.json`，不同作業系統與瀏覽器環境容易造成安裝或登入失敗，也要求學員交付高度敏感的 Google 工作階段資料。為了讓學員能在家自行完成設定，需要改為「分享自己的 Notebook 給課程專用 Gmail，再貼上 NotebookLM 網址」的免安裝綁定流程。

## What Changes（變更內容）

- 管理者設定一個一般 Gmail 作為課程專用 NotebookLM 帳號，並只在伺服器端完成一次登入與後續重新驗證。
- 課程帳號授權採可持久續期的 `storage_state`；每次 NotebookLM 操作或健康檢查產生的 Cookie 輪替，必須驗證後以加密資料與 revision/CAS 寫回，不能隨臨時檔刪除。
- 學員將自己建立的 NotebookLM 私下分享給指定課程帳號，權限設為檢視者。
- 學員在設定頁貼上自己的 NotebookLM 網址；系統擷取 Notebook ID、驗證課程帳號的存取權，並執行最小聊天測試。
- 綁定頁同時接受 Google 分享頁目前產生的 `notebook.google.com/notebook/<id>`，以及既有 `notebooklm.google.com`／`notebooklm.google` 官方網址，學員不需手動改寫網域。
- 系統保存 `LINE Channel ID → Notebook ID` 對應，不再保存每位學員的 Google Cookie。
- LINE 回答移除無法在聊天室操作的 NotebookLM 數字引用與無連結來源檔名，不另外傳送「參考來源」訊息。
- 長時間查詢使用 LINE Loading Animation 顯示進度，最長 60 秒並在回答尚未完成時定期續期；只有動畫 API 失敗才傳送文字等待提示。
- 設定頁提供明確的分享說明、連線測試、成功狀態，以及未分享、網址錯誤、已撤銷存取與課程帳號失效等錯誤訊息。
- 管理者後台顯示課程 NotebookLM 帳號健康狀態，並在登入失效時提供安全的重新驗證流程。
- 管理者可在學員清單勾選單一、複數或全部目前學員，批次設定或取消各 Channel 的個別到期時間；之後才完成綁定的學員不會自動繼承既有日期。
- 管理者可替手動產生、原本沒有姓名的邀請碼或已綁定學員補填、修改及清除姓名標籤。
- 管理者後台的學員與未使用邀請碼清單每頁顯示 10 筆並提供頁碼切換；管理者可在邀請碼旁複製未使用邀請碼，或刪除不再需要的邀請碼，已使用邀請碼不得刪除。
- 學員到期後，系統只清除該學員在本服務中的邀請碼、設定工作階段、LINE Channel、Notebook 對應及待處理事件；LINE Webhook 靜默接受但不再回答，且不會刪除或取消分享 Google NotebookLM 內的原始 Notebook。
- 綁定、狀態、列出與切換 Notebook 等操作必須驗證學員的一次性邀請碼或綁定權杖，並限制正式前端來源。
- 鎖定經 PoC 驗證的 `notebooklm-py` 版本，避免部署時自動升級造成非預期中斷。
- **BREAKING**：移除學員端上傳或貼上 `storage_state.json` 的主要流程；既有綁定必須先將 Notebook 分享給課程帳號，才能轉換到新的集中式授權模式。

## Capabilities（能力範圍）

### New Capabilities（新增能力）

- `shared-notebook-binding`：定義學員透過私下分享與 NotebookLM 網址完成綁定、驗證、狀態檢查及解除綁定的行為。
- `course-notebook-account`：定義管理者設定課程專用一般 Gmail、維護伺服器端 NotebookLM 授權、健康檢查與重新驗證的行為。
- `secure-channel-setup`：定義 LINE Channel 與 NotebookLM 設定 API 的身分驗證、敏感資料保護、來源限制及錯誤遮蔽要求。

### Modified Capabilities（修改既有能力）

目前 `openspec/specs/` 尚無既有能力規格，因此本變更沒有修改既有規格。

## Impact（影響範圍）

- 前端：`frontend/src/pages/SetupPage.tsx`、`frontend/src/pages/AdminPage.tsx`、設定 API 呼叫與綁定狀態畫面。
- 後端：NotebookLM 綁定模型與路由、課程帳號授權服務、NotebookLM 查詢服務及 LINE Webhook 錯誤處理。
- 資料庫：新增課程帳號設定／健康狀態資料及授權 revision；Channel 保留 Notebook ID，但淘汰每位學員的加密 NotebookLM Cookie。輪替後的課程帳號授權會以比較並交換方式加密寫回。
- 管理後台與資料庫：每個 Channel 保存獨立到期時間，邀請碼保存可編輯的學員姓名；清單以前端每頁 10 筆呈現並提供頁碼切換；未使用邀請碼可由管理員複製或刪除，刪除 API 會保護已使用邀請碼及其綁定資料；批次日期欄位只作為本次選取操作的輸入，不作為新學員預設值。
- 安全：綁定權杖、CORS allowlist、固定加密金鑰、敏感資料紀錄遮蔽與速率限制。
- 相依套件：鎖定 `notebooklm-py` 的驗證版本，並遵循其非官方 API 可能變更的維護風險。
- 營運：課程帳號成為集中式依賴，需要每 15 分鐘健康刷新、授權寫回失敗告警、登入失效重新驗證與 NotebookLM 用量監控。
- 測試：一般 Gmail 私下分享、Viewer Notebook 問答、多學員隔離、撤銷分享、並發查詢、登入失效與 LINE 非同步回覆。
