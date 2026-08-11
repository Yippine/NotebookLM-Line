## 1. 實作

- [x] 1.1 在 `scripts/nlm_cookie_refresh.py` 的 `main()` 成功路徑（`log(f"done — refreshed {len(channel_ids)} channel(s)")` 那一行之後）新增一次 `send_admin_alert` 呼叫，內容包含本次成功續期的 channel 數量
- [x] 1.2 確認「沒有任何已綁定 channel」（`bound_channel_ids()` 回傳空清單）時維持現狀，不呼叫 `send_admin_alert`——沒有實際動作可回報
- [x] 1.3 確認既有三個失敗路徑（cookie 讀取失敗、`storage_state.json` 讀取失敗、個別 channel rebind 失敗）的通知內容與行為維持不變，不因這次改動受影響

## 2. 測試

- [x] 2.1 新增測試：所有已綁定 channel 都成功 rebind 時，`send_admin_alert` 被呼叫一次，訊息內容包含正確的成功數量
- [x] 2.2 新增測試：沒有已綁定 channel 時，`send_admin_alert` 完全不會被呼叫
- [x] 2.3 回歸測試：既有三個失敗情境（cookie 讀取失敗、檔案讀取失敗、部分 channel rebind 失敗）的通知內容維持原本行為不變
- [x] 2.4 執行 `pytest` 全部測試，確認沒有破壞其他既有測試

## 3. 文件

- [x] 3.1 更新 `scripts/nlm_cookie_refresh.py` 檔案開頭的 docstring，說明現在成功執行也會發送通知，並提醒每小時執行一次會帶來對應的訊息量
- [x] 3.2 檢查 `docs/ubuntu-cookie-refresh-runbook.md` 是否有需要同步更新的告警行為描述
