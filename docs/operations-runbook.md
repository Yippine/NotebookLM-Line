# V2 部署、回滾與故障排除

## 環境隔離

新版應以獨立服務部署，例如：

- 管理者：`https://ai-notebook-v2.leopilot.com/admin`
- 學員：`https://ai-notebook-v2.leopilot.com/setup`
- Git 分支：`feature/shared-notebook-binding`

本主機的隔離部署使用：

- Compose project：`nlm-line-v2`
- loopback origin：`http://127.0.0.1:8085`
- Cloudflare Tunnel：`ai-notebook-v2`
- ingress 設定：`cloudflared-v2-config.yml`
- 使用者服務：`cloudflared-ai-notebook-v2.service`（已啟用 linger，可隨主機啟動）

檢查指令：`systemctl --user status cloudflared-ai-notebook-v2.service`、`docker compose -f docker-compose.v2.yml ps`。V2 使用獨立 Tunnel，不需重新啟動現行 `ai-notebook` Tunnel。

在驗收完成前，不要將現行 LINE Channel 的 Webhook 指向 V2。V2 必須使用獨立的：

- SQLite 資料庫與備份路徑
- Fernet 加密金鑰
- 管理員密碼與邀請碼
- 課程 Gmail 授權
- 測試 LINE Channel、Channel Secret、Access Token 及 Webhook
- HTTPS origin allowlist

同一主機部署時，V2 也必須使用不同的 Compose project、container 名稱／network、資料 volume 與對外 port。
Compose 預設只把 V2 port 綁在 `127.0.0.1`；Cloudflare Tunnel 應連到
`http://127.0.0.1:8085`，不要把該 port 直接開放到公網，否則不得信任轉送的
`X-Forwarded-For`／`X-Forwarded-Proto`。

V2 目前是單一 backend process／replica 架構：SQLite durable outbox 的啟動 claim 復原與 Notebook 對話鎖都以單一程序為前提。不得增加 Uvicorn workers 或同時啟動多個 V2 backend replicas；若要水平擴充，必須先改用外部工作佇列與分散式鎖。

`backend/.env.v2` 與 V2 資料目錄只能讓部署帳號讀取（建議檔案 `0600`、目錄 `0700`）。Docker daemon／socket 的存取等同於可讀取 runtime 環境中的 Fernet key 與管理員密碼，不得開放給不受信任帳號、共用面板或備份代理。

## 上線前必要設定

- `ENVIRONMENT=production`
- `ENCRYPTION_KEY`：固定 Fernet key；遺失後既有秘密資料無法復原。
- `DB_PATH`：V2 獨立資料庫路徑。
- `V2_ENV_FILE`：只在 Compose 需要改用其他 V2 設定檔時指定；未設定時讀取 `backend/.env.v2`。正式環境不得指向範例檔。
- `V2_DATA_DIR`：V2 專用資料目錄，未設定時使用 `./data-v2`。Compose 只會對容器內這個明確的專用 mount 修正為 `0700`，後端仍會在開啟 SQLite 前驗證目錄權限並將 DB 設為 `0600`。
- `ADMIN_PASSWORD`：長且唯一，不得沿用範例值。
- `WEBHOOK_BASE_URL`：V2 公開 HTTPS origin。
- `CORS_ALLOWED_ORIGINS`：只列 V2 正式 HTTPS origin，不使用 `*`。
- Cloudflare 必須為 V2 hostname 啟用 [Always Use HTTPS](https://developers.cloudflare.com/ssl/edge-certificates/additional-options/always-use-https/) 或等效 Redirect Rule；Edge Certificate 本身不代表 HTTP 一定會被拒絕。上線前以 `curl -I http://ai-notebook-v2.leopilot.com/admin` 確認回應為 301／308 且導向 HTTPS。nginx 同時送出一年期 HSTS，但不能取代第一次連線的邊緣重新導向。
- `NOTEBOOK_HOST_ALLOWLIST`：只允許已驗證的 NotebookLM 官方 host。
- `SHARED_NOTEBOOK_BINDING_ENABLED=true`
- `LEGACY_NLM_BINDING_ENABLED=false`；只有受控遷移時才暫時啟用。
- `NOTEBOOK_CHAT_TIMEOUT_SECONDS` 是 NotebookLM 串流聊天的單次讀取上限，預設 180 秒；`NOTEBOOK_QUERY_TIMEOUT_SECONDS` 是包含 client 建立、聊天、對話清理與授權寫回的整體上限，預設 210 秒，必須至少多保留 30 秒生命週期餘裕。
- `LINE_EVENT_CLAIM_TIMEOUT_SECONDS` 至少為三倍 `NOTEBOOK_QUERY_TIMEOUT_SECONDS` 再加 60 秒；目前預設 720 秒，過短會被拒絕啟動。
- `LINE_WEBHOOK_MAX_BODY_BYTES` 與 `LINE_WEBHOOK_MAX_EVENTS_PER_REQUEST` 限制單次 Webhook 的原始 body 與事件數；超過時會在寫入工作帳本前拒絕，nginx 的 `/webhook/` 也設為 1 MiB 上限。
- `LINE_EVENT_PENDING_LIMIT` 與 `LINE_EVENT_PENDING_PER_CHANNEL_LIMIT` 分別限制全站與單一 Channel 的加密待處理工作；單一 Channel 滿載時不會占用所有學員容量。
- `LINE_EVENT_REPLAY_PER_CHANNEL_BATCH`、`LINE_EVENT_MAX_ATTEMPTS`、`LINE_EVENT_RETRY_BASE_SECONDS` 與 `LINE_EVENT_REPLAY_INTERVAL_SECONDS` 控制公平分批、重試及排程；帳本滿載時 Webhook 回 503，不接受無界工作。
- `LINE_EVENT_PENDING_RETENTION_SECONDS` 控制未完成提問密文的最長保留時間，V2 預設且最高為 86400 秒，以配合 LINE retry key 的有效期間。
- `LINE_BACKGROUND_SHUTDOWN_TIMEOUT_SECONDS` 是包含一般等待與取消清理在內的硬上限；仍未結束的工作由程序停止，已加密的待處理內容保留在 V2 資料庫並於下次啟動解除 claim 後恢復，不依賴 LINE 重送。
- Compose 的 backend `stop_grace_period` 必須大於上述背景關機等待值；範例使用 30 秒對應預設 15 秒。若提高背景等待時間，也要同步提高 Docker grace period。
- `TRUST_PROXY_HEADERS=true` 只適用於 backend 不對外公開、且僅接受受信任 nginx 流量的部署；本方案由 loopback-only Cloudflare Tunnel 導入，nginx 固定轉送 `X-Forwarded-Proto: https`，不採信用戶端送入的欄位。
- Compose 對 backend／frontend 的 `json-file` log 設有 10 MiB、3 個檔案的輪替上限；部署平台若覆寫 logging driver，必須提供等效的容量與保留限制。

## 部署順序

1. 備份正式資料庫，確認可還原。
2. 從新分支建立獨立 V2 image，不覆蓋正式 tag。
3. 啟動 V2 後執行資料庫遷移測試與 `/health` 檢查。
4. 設定課程 Gmail 並確認狀態 `healthy`。
5. 使用測試邀請碼及測試 LINE Channel 完成綁定。
6. 驗證 Webhook 簽章錯誤會被拒絕，正常事件快速回 2xx，慢速回答使用背景 Push，且在待處理期間強制停止服務後能由加密工作恢復完成。
7. 完成跨 Channel、撤銷分享、帳號過期、限流與 CORS 測試後才開放 V2 網址。

資料庫備份必須使用 SQLite online backup API 或停機後的一致性快照，不可在寫入中直接複製單一 DB 檔。備份可能包含當下尚未完成的提問密文；必須使用加密且限制存取的獨立路徑、盤點 pending 工作、設定備份保留／銷毀期，並與 Fernet 金鑰分開保管。還原測試只能在隔離環境使用原金鑰進行。

## 回滾

1. 停止導入新學員，保留事件與 request ID。
2. 關閉 `SHARED_NOTEBOOK_BINDING_ENABLED` 或將流量切回上一個 V2 image。
3. 不要把 V2 資料庫直接覆蓋正式資料庫。
4. 若資料遷移尚未清除舊 Cookie，可在管理員限定模式暫時回到舊流程；已清除的 Cookie 不得重新向學員收集。
5. 從部署前備份還原時，先在隔離環境確認 Channel、邀請碼、到期時間與 Notebook ID。

## 故障分類

- 只有一個 Notebook 失敗：檢查是否被刪除、網址錯誤或取消分享，標示 `access_revoked`。
- 所有 Notebook 同時失敗：檢查課程帳號狀態；若為授權拒絕，重新驗證。
- 逾時或 429：保持原綁定，降低並發或等待重試；不要誤判為撤銷分享。
- LINE 無回覆：先確認 Webhook 簽章、快速 2xx、背景工作、Push 權限與 Token 是否有效。
- 加密待處理工作累積：檢查 NotebookLM／LINE 上游狀態、重試次數與排程器；不得直接在 SQLite 或 log 輸出、解密或複製問題內容。
- 解密失敗：確認部署使用原固定 Fernet key；不要產生新 key 覆蓋。

## 不可逆 Cookie 清除

每個 Channel 新式綁定成功且抽查 LINE 問答後，才可清除 `nlm_auth_json_encrypted`。清除前需備份；清除後不得再透過學員介面收集 Cookie，若需恢復服務應使用課程 Gmail 集中授權。
