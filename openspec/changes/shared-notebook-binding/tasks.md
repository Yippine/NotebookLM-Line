## 1. PoC 與相依版本確認

- [ ] 1.1 準備一個課程專用一般 Gmail、兩個測試學員 Gmail，以及內容可明確區分的兩本測試 Notebook
- [ ] 1.2 驗證普通 Gmail 可將 Notebook 以 Viewer 分享給課程帳號，並記錄是否需要手動接受邀請及實際 Notebook URL 格式
- [ ] 1.3 使用目前候選 `notebooklm-py` 版本對兩本 Viewer shared Notebook 執行 `notebooks.get`、`chat.ask` 與引用來源測試
- [ ] 1.4 驗證取消分享、刪除 Notebook、課程帳號登出、限流及暫時性上游錯誤的實際錯誤型別
- [ ] 1.5 測量 shared Notebook 回答延遲、同時查詢表現及用量歸屬，決定初始並發上限與逾時值
- [x] 1.6 決定第一版採「加密資料庫 storage state＋短效臨時檔＋revision/CAS 寫回」，durable master token 延後至獨立安全評估
- [ ] 1.7 將 PoC 通過的 `notebooklm-py` 精確版本鎖定於後端相依檔，並建立升級檢查說明

## 2. 資料模型與設定基礎

- [x] 2.1 新增 `course_notebook_account` 單例資料表，包含 email、加密授權、授權模式、健康狀態、最後成功／檢查時間及遮蔽錯誤碼
- [x] 2.2 新增持久化 `setup_sessions` 與 `admin_sessions` 資料表，只保存 token 雜湊、範圍、到期與撤銷資訊
- [x] 2.3 擴充 `channels` 綁定欄位，保存 Notebook ID、顯示名稱、綁定狀態、綁定時間與最後存取檢查時間
- [x] 2.4 新增 LINE Channel Secret 與 Access Token 的加密欄位及可回滾的資料遷移程序
- [x] 2.5 擴充設定模型，加入正式前端 CORS allowlist、Notebook host allowlist、工作階段期限、查詢並發上限、逾時與功能旗標
- [x] 2.6 修改加密服務，使正式環境缺少或無法解析固定 Fernet 金鑰時立即拒絕啟動
- [x] 2.7 建立資料庫遷移與回滾測試，確認既有 Channel、邀請碼、到期時間及 Notebook ID 不遺失

## 3. 設定與管理員身分驗證

- [x] 3.1 實作 setup token 的產生、雜湊、持久化、期限、撤銷及邀請碼／Channel 範圍驗證
- [x] 3.2 將學員受保護 API 改為解析 `Authorization: Bearer`，並拒絕以 query string 傳送 token
- [x] 3.3 實作管理員登入端點，從 HTTPS request body 驗證密碼並簽發短效管理員 Bearer token
- [x] 3.4 將所有管理 API 改為管理員 Bearer token，移除 query string 中的 `admin_password`
- [x] 3.5 對邀請碼、管理員登入、Channel 更新、Notebook 綁定與課程帳號重新驗證加入可設定速率限制
- [x] 3.6 將 CORS 從萬用來源改為設定中的正式 HTTPS origin allowlist，並保留 LINE Webhook 的公開簽章驗證
- [x] 3.7 加入 request ID、資源識別碼遮蔽與敏感欄位過濾，測試 log 不含邀請碼、Bearer token、管理員密碼、LINE 憑證或 Google 授權

## 4. 課程 NotebookLM 帳號服務

- [x] 4.1 建立課程帳號 repository 與 service，支援讀取 email、加密保存授權及原子替換有效授權
- [x] 4.2 實作集中式 NotebookLM client factory，移除每 Channel 覆寫 `NOTEBOOKLM_AUTH_JSON` 的全域環境變數流程
- [x] 4.3 以可設定 semaphore 與有界等待控制 NotebookLM 查詢並發，確保每次請求使用正確 Notebook context
- [x] 4.4 實作課程帳號健康檢查與 `unconfigured`、`healthy`、`expired`、`error` 分類
- [x] 4.5 實作管理員課程帳號設定、狀態查詢、按需健康檢查及安全重新驗證 API
- [x] 4.6 為課程帳號查詢加入不含問題內容與秘密值的成功率、延遲、限流及錯誤分類指標
- [x] 4.7 實作授權失效時的管理員狀態與學員通用錯誤，確認不會將課程帳號授權內容傳回前端
- [x] 4.8 將 `notebooklm-py` 的 `Authentication expired or invalid`、HTTP 401 與等價訊號穩定分類為 `course_auth_expired`，並將逾時、限流及 5xx 保持為暫時性錯誤
- [x] 4.9 在每次 NotebookLM client 結束前讀回更新後的 storage state，驗證必要 Cookie，並以原始 `auth_revision` 進行加密 CAS 寫回
- [x] 4.10 讓一般查詢與每 15 分鐘健康檢查都能保存 Cookie 輪替，確保短效臨時檔只在受控範圍存在且結束後必定刪除
- [x] 4.11 新增無變更不寫入、並行 revision 衝突、寫回失敗、重新驗證競爭及容器重啟後沿用最新版授權的測試
- [x] 4.12 新增不含 Cookie／storage state 的 `auth_persistence_failed` 健康狀態、指標與告警，且回答成功時不因單純寫回失敗而遺失回答

## 5. 共享 Notebook 綁定後端

- [x] 5.1 實作 Notebook URL parser，只接受 HTTPS 與 host allowlist，並以單元測試涵蓋有效、重新品牌、錯誤及惡意網址
- [x] 5.2 新增 URL 綁定 request／response model，回傳正規化狀態、Notebook 顯示名稱、最後檢查時間與 request ID
- [x] 5.3 實作「測試並綁定」流程：先執行 `notebooks.get` 與固定最小聊天測試，成功後才原子更新 Channel 映射
- [x] 5.4 實作綁定失敗的錯誤分類，區分網址錯誤、尚未分享、存取撤銷、課程帳號失效、限流與未知錯誤
- [x] 5.5 實作受 Channel 所有權保護的綁定狀態、立即重新檢查、重新綁定及解除綁定 API
- [x] 5.6 修改 LINE 問答服務，統一使用課程帳號授權及 Channel 的 Notebook ID，不再讀取每學員加密 Cookie
- [x] 5.7 以 Channel ID 與 LINE user ID 隔離對話 context，避免不同使用者或 Channel 共用 NotebookLM 對話歷史
- [x] 5.8 在實際問答發現撤銷分享或帳號失效時更新健康狀態，並透過既有 Loading＋背景 Push 流程回覆繁體中文指引
- [x] 5.9 將舊 `/nlm-login` 與 `/nlm-bind-local` 限制為功能旗標下的管理員遷移工具，停止提供學員公開存取

## 6. 學員與管理員前端

- [x] 6.1 重構前端 API client，所有學員與管理員受保護呼叫皆使用 Authorization Header
- [x] 6.2 將 NotebookLM 設定步驟改為顯示課程 Gmail、Viewer 分享步驟、資料可見性說明與 Notebook URL 欄位
- [x] 6.3 移除學員端 `storage_state.json` 檔案選擇、JSON textarea 與相關解析程式
- [x] 6.4 實作「測試並綁定」載入狀態，告知會產生一次測試查詢並防止重複提交
- [x] 6.5 顯示 `unbound`、`checking`、`bound`、`access_revoked`、`course_account_unavailable`、`error` 的繁體中文狀態與可執行下一步
- [x] 6.6 新增重新檢查、重新綁定、解除綁定與取消 NotebookLM 分享的操作指引
- [x] 6.7 在管理後台新增課程帳號 email、健康狀態、最後檢查、按需檢查與重新驗證操作
- [x] 6.8 確認前端不呈現或持久保存 Google 授權、LINE Secret、Access Token、管理員密碼與完整錯誤內容

## 7. 既有資料遷移與舊流程下線

- [ ] 7.1 以功能旗標部署新綁定 API 與畫面，先讓測試 Channel 驗證而不影響既有 LINE 問答
- [x] 7.2 建立既有 Channel 遷移狀態，要求學員分享目前 Notebook 並完成 URL 驗證
- [x] 7.3 在新式綁定成功後保留 Notebook ID 並清除該 Channel 的 `nlm_auth_json_encrypted`
- [ ] 7.4 加密遷移既有 LINE Secret 與 Access Token，驗證 Webhook 後再清理明文資料
- [ ] 7.5 過渡期結束後移除公開 JSON／本機綁定端點及前端死碼，確認系統不再收集學員 Google Cookie
- [ ] 7.6 撰寫並演練資料庫備份、功能旗標回滾、課程帳號重新驗證及不可逆 Cookie 清除程序

## 8. 測試、驗收與營運文件

- [x] 8.1 為 URL parser、token 範圍、session 到期、加密、錯誤分類、Channel 映射與狀態機新增後端單元測試
- [x] 8.2 建立多 Channel 整合測試，證明 Channel A／B 只查詢各自 Notebook，重新綁定失敗不覆蓋原綁定
- [x] 8.3 建立安全回歸測試，涵蓋跨 Channel 存取、query string 秘密值、錯誤 CORS、無效 LINE 簽章、速率限制與 log 洩漏
- [x] 8.4 建立前端測試，涵蓋分享指引、URL 驗證、各狀態訊息、重新檢查、解除綁定與工作階段過期
- [ ] 8.5 使用真實測試帳號完成端到端驗收：學員分享、貼 URL、LINE 提問、引用來源、取消分享、重新分享及管理員重新驗證
- [x] 8.6 執行慢速與並發測試，確認 Webhook 快速回傳、Loading 動畫及背景 Push 不依賴過期 Reply Token
- [x] 8.7 更新 README、部署環境變數、管理員操作手冊、學員圖文指引、隱私告知、故障排除與版本升級清單
- [x] 8.8 執行完整測試、前端建置、OpenSpec 驗證與正式部署前安全檢查，記錄驗收結果
- [ ] 8.9 使用真實課程 Gmail 連續至少 48 小時驗證閒置健康刷新、實際問答、容器重啟及授權 revision 持續更新，確認不需人工重新貼授權 JSON

## 9. 管理後台學員生命週期

- [x] 9.1 在 Channel 保存個別到期時間，讓未設定期限、不同期限的學員可同時存在
- [x] 9.2 在學員清單加入逐列勾選與表頭全選，批次設定只更新當下勾選的 Channel
- [x] 9.3 實作只取消勾選學員期限的操作，並保留其邀請碼、LINE Channel 與 Notebook 綁定
- [x] 9.4 確保後續新增或才完成綁定的學員預設無期限，不繼承前一次批次日期
- [x] 9.5 在學員清單逐列顯示到期時間，支援不同學員使用不同日期
- [x] 9.6 實作到期排程，只刪除已到期 Channel 的設定工作階段、已使用邀請碼、Notebook 映射、LINE 憑證與待處理事件
- [x] 9.7 讓已刪除 Channel 的 LINE Webhook 靜默回傳 2xx，且不執行 NotebookLM 查詢或 LINE Push
- [x] 9.8 允許管理員在未使用邀請碼與已綁定學員清單補填、修改或清除姓名
- [x] 9.9 新增批次日期隔離、到期清理、Webhook 靜默處理及姓名編輯的後端與前端測試

## 10. LINE 回答呈現與長查詢進度

- [x] 10.1 移除 LINE 回答正文中的 `[1]`、`[1, 2]` 等 NotebookLM 純數字引用
- [x] 10.2 停止額外查詢及傳送只有來源檔名、沒有可操作連結的參考來源訊息
- [x] 10.3 修正 LINE Loading Animation 端點為 `/v2/bot/chat/loading/start`，並將官方 `202` 回應視為成功
- [x] 10.4 首次顯示 60 秒 Loading Animation，回答未完成時每 50 秒續期 60 秒
- [x] 10.5 保留 Loading API 失敗時的文字等待提示，並確保失敗不影響背景回答與 Push
- [x] 10.6 新增引用格式、Loading API 與續期行為測試，並執行完整後端回歸測試

## 11. Google 分享網址相容性

- [x] 11.1 前後端精確接受 Google 分享頁產生的 `notebook.google.com/notebook/<id>`，保留既有官方網域相容性並拒絕偽造子網域
- [x] 11.2 更新輸入範例、後端設定、前後端回歸測試與 OpenSpec 規格，讓學員不需自行改寫分享網址
