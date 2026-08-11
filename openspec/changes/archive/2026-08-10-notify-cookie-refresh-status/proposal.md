## Why

`nlm_cookie_refresh.py` 目前只有在失敗時才會通知管理員（cookie 讀取失敗、或 rebind 失敗）。這代表排程正常運作時完全沒有任何回饋——管理員沒有辦法單純從 LINE 上確認「這個小時的自動續期有沒有真的跑、有沒有成功」，只能自己去開 Windows 工作排程器或看伺服器 log。既然已經把排程頻率調整到每小時一次，讓每次執行的結果（不只是失敗）都回報給管理員，能讓這個機制的運作狀況變得可觀察，而不是狀態不明的黑盒子。

## What Changes

- `nlm_cookie_refresh.py` 在每次執行完成後（不論成功或失敗）都會發送一則 LINE 通知給管理員
- 成功時：簡短的一行摘要（例如成功續期了幾個 channel）
- 失敗時：沿用現有的詳細失敗訊息（cookie 讀取失敗、或個別 channel rebind 失敗的清單），不改動既有行為
- 成功摘要刻意保持精簡（一行），因為每小時都會送一次，累積起來一天有 24 則，訊息本身要夠短，才不會變成單純的噪音

## Capabilities

### New Capabilities
- `cookie-refresh-notifications`：定義 `nlm_cookie_refresh.py` 每次執行完成後，不論成功或失敗，都必須通知管理員目前執行結果的行為。

### Modified Capabilities
（無——目前專案尚未有既有的 spec 涵蓋 cookie 續期通知的行為，這是全新能力。）

## Impact

- `scripts/nlm_cookie_refresh.py`：`main()` 在成功路徑新增一次 `send_admin_alert` 呼叫；失敗路徑維持不變
- 不涉及資料庫或後端 API 變更，純粹是這支獨立排程腳本自己的行為
- 不需要新增依賴套件（沿用既有的 `send_admin_alert`/`.env` 讀取邏輯）
- 執行頻率已是每小時一次，此變更會讓管理員每天多收到最多 24 則成功摘要訊息（加上原本就有的失敗告警）
