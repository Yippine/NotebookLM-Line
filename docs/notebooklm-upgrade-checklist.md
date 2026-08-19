# `notebooklm-py` 版本鎖定與升級清單

`notebooklm-py` 是非官方整合。正式相依檔必須鎖定精確 stable 版本；2026-08-18 查核到的候選 stable 為 `0.8.1`，但在真實 Viewer shared Notebook PoC 通過前，不得標示為已驗證版本。

從 0.7.x 升到 0.8.x 時，`chat.delete_conversation()` 成功後改為回傳 `None`，失敗則拋出例外；呼叫端不得再以布林回傳值判斷成功。0.8.1 的自動 chat timeout 也會以一般 HTTP timeout 作為下限，因此本服務必須明確傳入獨立的 `chat_timeout`，並在外層查詢期限保留清理與授權寫回餘裕。

## 第一次鎖定前

- 記錄套件版本、tag／commit、Python 版本及測試日期。
- 以課程 Gmail 測試自有 Notebook 的 `notebooks.get`、`chat.ask` 與引用來源。
- 以兩個不同學員 Gmail 分享兩本內容明確不同的 Viewer Notebook，重複上述測試。
- 測試取消分享、刪除 Notebook、完整登出、授權過期、429、網路逾時及 5xx 錯誤分類。
- 測量單次延遲與並發，設定 semaphore、等待上限及 request timeout。
- 驗證套件的 storage file／profile 可在部署環境安全持久化及更新 Cookie。

## 每次升級

1. 閱讀 release notes 與 breaking changes，特別注意 auth、exception hierarchy、conversation semantics、Notebook decoder 和 citation 格式。
2. 在獨立分支只更新精確版本，不使用無上限版本範圍，也不直接採用 alpha／beta／rc。
3. 執行後端單元、安全與多 Channel 整合測試。
4. 用真實測試帳號執行自有 Notebook＋Viewer shared Notebook smoke test。
5. 驗證 `conversation_id` 不會跨 Channel 或 LINE user 共用。
6. 驗證慢速回答仍快速回 Webhook 2xx，並由背景 Push 完成。
7. 部署到 V2 隔離環境觀察錯誤率、延遲與 429，再決定推進。

## 回滾證據

保留前一版 lockfile／image、資料庫備份、測試輸出及已知可用授權格式。若升級後 decoder 或 auth 失敗，立即回到前一精確版本，不要在正式環境即席修改 Cookie 或 RPC payload。
