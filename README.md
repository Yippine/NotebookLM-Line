# NotebookLM LINE Bot Platform

讓課程學員各自擁有 LINE 官方帳號，並把自己的 NotebookLM 私下分享給課程專用 Gmail；學員只要貼上 Notebook URL，就能讓使用者透過 LINE 聊天提問。

## 架構

```
學員 Notebook ──Viewer 分享──→ 課程專用 Gmail
                                      │
使用者 LINE → Cloudflare Tunnel → FastAPI → notebooklm-py → 背景 Push 回覆
```

- 一台 Server 接收多個 LINE 官方帳號的 Webhook（`/webhook/{channel_id}`）
- React 管理介面讓學員用邀請碼自助設定，不需要安裝本機套件
- SQLite 保存加密 LINE 憑證、短效工作階段、Channel → Notebook ID 映射、一份加密的課程 Gmail 授權，以及會在完成／終止後清除內容的加密 LINE 待處理工作
- 設定與管理 API 使用具範圍的 Bearer token；LINE Webhook 維持簽章驗證
- NotebookLM 消費者版沒有適用此流程的正式公開聊天 API，本專案使用非官方 `notebooklm-py`，部署前必須用真實測試帳號驗收

## 快速開始

### 1. 後端

```bash
cd backend
pip install -r requirements.txt
```

複製環境設定並產生固定加密金鑰：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

請參考 [`backend/.env.example`](backend/.env.example)。正式環境若缺少或無法解析固定 Fernet key，後端會拒絕啟動；請妥善備份，遺失後既有秘密資料無法解密。

啟動：

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

管理員先用密碼換取短效 token，再呼叫管理 API：

```bash
ADMIN_TOKEN=$(curl -sS -X POST http://localhost:8000/api/admin/login \
  -H 'Content-Type: application/json' \
  -d '{"password":"<管理員密碼>"}' | jq -r .token)

curl -X POST 'http://localhost:8000/api/invite-codes/generate?count=40' \
  -H "Authorization: Bearer ${ADMIN_TOKEN}"
```

### 2. 前端

```bash
cd frontend
npm install
npm run dev      # 開發模式（proxy 到 localhost:8000）
npm run build    # 產出靜態檔到 dist/，FastAPI 自動 serve
```

### 3. Cloudflare Tunnel

```bash
cloudflared tunnel --url http://localhost:8000
```

將產生的 URL 設為 `.env` 中的 `WEBHOOK_BASE_URL`。

### 3. V2 獨立環境

要讓新版和現行服務並行，請使用獨立的 V2 設定與 Compose project：

```bash
install -m 600 backend/.env.v2.example backend/.env.v2
install -d -m 700 data-v2
# 填入 V2 專用資料庫、Fernet key、管理員密碼及網址
stat -c '%a %n' backend/.env.v2 data-v2
docker compose -f docker-compose.v2.yml up --build -d
```

`stat` 應顯示設定檔為 `600`、資料目錄為 `700`；後端也會在 production 啟動時強制資料庫檔為 `600`。此主機的 V2 port 使用 `8085`，避開既有服務占用的 `8084`。正式與 V2 不得共用資料庫、加密金鑰、邀請碼、課程 Gmail 授權或 LINE 測試 Channel。更多說明見 [`docs/operations-runbook.md`](docs/operations-runbook.md)。

## 管理者第一次設定

1. 準備一個只供課程使用的一般 Gmail，先直接登入 NotebookLM。
2. 在受信任電腦取得該帳號的 NotebookLM `storage_state.json`。
3. 登入 `/admin`，設定課程 Gmail 與授權並確認健康狀態為 `healthy`。
4. 產生邀請碼供學員使用。

這不是 Google Workspace、Service Account 或官方 NotebookLM OAuth。只在管理者端維護一份課程帳號授權；學員不需、也不應交付自己的 Cookie。詳見 [`docs/admin-guide.md`](docs/admin-guide.md)。

## 學員設定流程

1. 打開管理介面，輸入邀請碼
2. 填入 LINE Channel ID、Channel Secret、Channel Access Token
3. 將顯示的 Webhook URL 貼到 LINE Developers Console
4. 在 NotebookLM 把自己的 Notebook 以 Viewer 分享給畫面顯示的課程 Gmail
5. 將自己的 Notebook URL 貼到設定頁，按「測試並綁定」
6. 測試成功後，直接在自己的 LINE 官方帳號提問

學員不需要安裝 Python、CLI 或 Chrome Cookie 擴充套件。完整圖文文字版見 [`docs/student-guide.md`](docs/student-guide.md)，資料可見性見 [`docs/privacy-notice.md`](docs/privacy-notice.md)。

## API 端點

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/api/verify-invite` | 驗證邀請碼 |
| POST | `/api/channels` | 使用 setup Bearer token 建立／更新 Channel |
| GET | `/api/channels/{id}` | 使用具 Channel 範圍的 setup token 查詢 Channel |
| GET | `/api/course-account/public` | 取得可分享的課程 Gmail 與非敏感狀態 |
| POST | `/api/channels/{id}/notebook-binding` | 測試 Notebook URL 並原子綁定 |
| GET | `/api/channels/{id}/notebook-binding` | 查詢綁定狀態 |
| POST | `/api/channels/{id}/notebook-binding/recheck` | 立即重新檢查分享權限 |
| DELETE | `/api/channels/{id}/notebook-binding` | 解除 Notebook 映射 |
| POST | `/api/admin/login` | 管理員密碼換取短效 Bearer token |
| GET / PUT | `/api/admin/course-account` | 查詢／設定課程 Gmail 授權 |
| POST | `/api/admin/course-account/health-check` | 立即檢查課程帳號 |
| POST | `/api/invite-codes/generate` | 管理員批量產生邀請碼 |
| POST | `/webhook/{channel_id}` | LINE Webhook |

受保護端點必須使用 `Authorization: Bearer <token>`；不得把 setup token、管理員密碼或管理員 token 放在 query string。

## 測試與版本升級

```bash
# 後端
pip install -r backend/requirements-test.txt
PYTHONPATH=backend python -m pytest backend/tests
ruff check backend
ruff format --check backend
pip-audit -r backend/requirements.txt

# 前端
cd frontend
npm test
npm run build
npm audit

# V2 Compose 設定
cd ..
docker compose -f docker-compose.v2.yml config --no-env-resolution -q
```

`notebooklm-py` 升級前必須完成自有 Notebook、Viewer shared Notebook、撤銷分享、多 Channel 路由、引用來源與慢速 LINE Push 測試。見 [`docs/notebooklm-upgrade-checklist.md`](docs/notebooklm-upgrade-checklist.md)。

目前的本機驗證結果與尚未解除的真實帳號上線條件，見 [`docs/verification-report.md`](docs/verification-report.md)。

## 貢獻與溝通準則

本專案歡迎任何形式的技術交流與改進建議。若您發現程式架構有需要改進之處，歡迎透過 [Issue](https://github.com/YwY170/notebooklm-line/issues) 或 [Pull Request](https://github.com/YwY170/notebooklm-line/pulls) 提出公開、具體、建設性的反饋。

> ⚠️ **本專案明確拒絕任何以私下或公開方式貶低、嘲諷原創作者開發瑕疵的行為。**  
> 每一段程式碼都是作者投入心力的成果，技術本就是在迭代中進步的。請以尊重與善意的方式進行交流，共同維護良好的開源協作環境。

> 🏳️‍🌈 **本專案支持性別友善，明確拒絕任何形式的性別攻擊與歧視。**  
> 無論性別認同或性別表達為何，每位貢獻者與使用者都應受到平等尊重。
