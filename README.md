# NotebookLM LINE Bot Platform

讓課程學員各自擁有 LINE 官方帳號，綁定 NotebookLM，使用者透過 LINE 聊天直接提問。

## 架構

```
使用者 LINE → Cloudflare Tunnel → FastAPI Server → notebooklm-py → 文字回覆
```

- 一台 Server 接收多個 LINE 官方帳號的 Webhook（`/webhook/{channel_id}`）
- React 管理介面讓學員自助設定
- SQLite 儲存 Channel 資訊與加密 cookie

## 快速開始

### 1. 後端

```bash
cd backend
pip install -r requirements.txt
```

產生加密金鑰並寫入 `.env`：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

```env
ENCRYPTION_KEY=your-generated-key
WEBHOOK_BASE_URL=https://your-tunnel-domain.com
```

啟動：

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

產生邀請碼（呼叫 API）：

```bash
curl -X POST http://localhost:8000/api/invite-codes/generate?count=40
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

## 學員設定流程

1. 打開管理介面，輸入邀請碼
2. 填入 LINE Channel ID、Channel Secret、Channel Access Token
3. 將顯示的 Webhook URL 貼到 LINE Developers Console
4. 在伺服器執行 `notebooklm login`，將 `storage_state.json` 內容貼到管理介面完成綁定

## API 端點

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/api/verify-invite` | 驗證邀請碼 |
| POST | `/api/channels` | 建立 Channel |
| GET | `/api/channels/{id}` | 查詢 Channel |
| POST | `/api/channels/{id}/nlm-login` | 綁定 NotebookLM |
| GET | `/api/channels/{id}/nlm-status` | 查詢綁定狀態 |
| POST | `/api/invite-codes/generate` | 批量產生邀請碼 |
| POST | `/webhook/{channel_id}` | LINE Webhook |

## 貢獻與溝通準則

本專案歡迎任何形式的技術交流與改進建議。若您發現程式架構有需要改進之處，歡迎透過 [Issue](https://github.com/YwY170/notebooklm-line/issues) 或 [Pull Request](https://github.com/YwY170/notebooklm-line/pulls) 提出公開、具體、建設性的反饋。

> ⚠️ **本專案明確拒絕任何以私下或公開方式貶低、嘲諷原創作者開發瑕疵的行為。**  
> 每一段程式碼都是作者投入心力的成果，技術本就是在迭代中進步的。請以尊重與善意的方式進行交流，共同維護良好的開源協作環境。

> 🏳️‍🌈 **本專案支持性別友善，明確拒絕任何形式的性別攻擊與歧視。**  
> 無論性別認同或性別表達為何，每位貢獻者與使用者都應受到平等尊重。
