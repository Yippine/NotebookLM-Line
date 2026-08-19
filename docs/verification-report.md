# Shared Notebook Binding 驗證紀錄

## 2026-08-18 V2 逾時與相依升級補充驗證

- `notebooklm-py` 候選版由 `0.7.3` 精確更新為 `0.8.1`，並依 0.8.x 契約將 `chat.delete_conversation()` 改為「無例外即成功」，不再判斷已移除的布林回傳值。
- 明確設定聊天讀取 180 秒、完整查詢生命週期 210 秒及 LINE event claim 720 秒；LINE Loading 仍每 50 秒續期 60 秒。
- 後端完整回歸為 181 passed；前端為 23 passed，production build 通過；Python 相依稽核無已知漏洞；OpenSpec strict validation 與 Compose 設定檢查通過。
- V2 重建後容器內確認 `notebooklm-py==0.8.1`、runtime 設定驗證成功、課程帳號健康檢查為 `healthy`。
- 以既有 Viewer shared Notebook 執行先前會超過 90 秒的實際聊天，124.5 秒取得完整回答，未再被舊外層期限提前取消；成功後 Channel 從 `error` 恢復為 `bound`。
- 部署後收到的三筆真實 LINE 事件均完成且沒有錯誤碼；第一筆 64 秒完成，後兩筆因在前一題完成前重複送出而包含同 Notebook 的隔離排隊時間。
- Channel ID、Secret 與 Access Token 現在由前端送出前及後端驗證／保存前移除首尾空白；舊資料在讀取 Secret／Token 時也會正規化。使用資料庫現有憑證產生簽章的公開 Webhook smoke test 回傳 200。

以上只核准本次 V2 修復；兩本 Viewer Notebook、撤銷分享、引用來源及 48 小時授權輪替等完整 PoC 尚未完成，因此 OpenSpec 1.7 仍保持未完成。

- 日期：2026-07-23
- 分支：`feature/shared-notebook-binding`
- 環境：本機隔離 V2 容器＋獨立 Cloudflare Tunnel
- 結論：**程式與部署封裝檢查通過，但尚未核准正式上線**

## 已通過

| 檢查 | 結果 |
|---|---|
| 後端單元／整合／安全回歸 | 175 passed |
| 前端元件與 API 測試 | 22 passed |
| Python Ruff lint／format | passed |
| Python compileall | passed |
| 前端 TypeScript＋Vite production build | passed |
| `pip-audit` | 0 known vulnerabilities |
| `npm audit` | 0 vulnerabilities |
| V2 Compose config | passed |
| nginx config | passed |
| 前後端 Docker image build | passed |
| V2 production 容器 health check | passed |
| 公開 HTTPS `/`、`/setup`、`/admin` | passed |
| 公開管理員登入／登出（未輸出秘密值） | passed |
| HTTP → HTTPS 301、HSTS 與安全標頭 | passed |
| 現行 `ai-notebook.leopilot.com` 無回歸 | passed |
| OpenSpec strict validation | passed |
| `git diff --check` | passed |

本次通過 smoke test 的本機 image ID：

- backend：`sha256:e2778145291fb70102454078dc72ba53ca9b8cbc4ef253167aad3ee6ac91a4f0`
- frontend：`sha256:8fa6f9e9179dc5078bd0d3e1bff876d935e95523c0f2151b316a8850322c0ea2`

以上為本機 content ID；實際發布到 registry 後仍須記錄 registry digest 與 SBOM。

已覆蓋的關鍵回歸包括：Bearer token 與 Channel scope、工作階段撤銷／到期、邀請碼到期、跨 Channel 存取、惡意 Notebook URL、CORS、LINE 簽章、Webhook body／事件數上限、快速回應、加密 durable outbox、全站與每 Channel 容量、公平 round-robin replay、穩定 LINE retry key、崩潰／重啟後 replay、`pending`／`completed` 去重、claim CAS、內部有限重試與有界帳本、硬上限關機取消、production DB 權限、課程帳號失效時的學員提示、邀請碼與其他敏感 log／422 回應遮蔽、舊綁定遷移、`checking` lease 逾時復原、有界 Notebook lock、重新綁定競態、同 Channel 並發問答及 Gmail／授權 revision 競態。

## 正式上線阻擋條件

以下項目需要真實 Google／NotebookLM／LINE 測試資源，不能由 mock 測試取代：

1. 使用課程專用一般 Gmail 與至少兩個學員 Gmail，驗證 Viewer 私人分享、邀請接受方式及實際 URL。
2. 對兩本可區分 Notebook 執行 `notebooks.get`、`chat.ask`、引用來源、取消／恢復分享、刪除 Notebook、登出、限流與暫時性錯誤測試。
3. 測量真實延遲、同時查詢與配額歸屬，再確認並發與逾時設定。
4. 已實作加密 storage state 的 revision/CAS 寫回、並行舊版本防覆蓋、持久化失敗健康狀態及重啟載入測試；仍須以真實課程 Gmail 連續至少 48 小時驗證每 15 分鐘刷新與實際 Cookie 輪替。
5. 授權檔若沒有 account email metadata，Gmail 身分比對只能由管理者人工確認；真實驗收必須確認實際格式是否能強制自動比對。
6. 用 V2 測試 LINE Channel 完成分享、貼 URL、LINE 提問、引用、取消分享、重新分享與管理員重新驗證的端到端測試。
7. 在隔離 V2 實際演練資料庫備份／還原、功能旗標回滾、LINE 明文憑證清理及不可逆舊 Cookie 清除。
8. 正式上線前產生完整 Python lock、固定 Docker 基底映像 digest，並保存本次核准 image digest／SBOM，確保之後可重建與可靠回滾；目前未鎖定的依賴不阻擋隔離 V2 試用。

## 上線判定

在上述阻擋條件完成前：

- 不得把現行正式 LINE Channel Webhook 切到 V2。
- 2026-07-23 原始驗證使用 `notebooklm-py==0.7.3`；2026-08-18 V2 已更新為候選 `0.8.1`，仍不是完整 PoC 核准版本。
- 不得執行舊 Google Cookie 或 LINE 明文憑證的不可逆清除。
- 現行正式服務與資料庫應維持不變。
