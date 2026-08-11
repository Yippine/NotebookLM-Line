## Purpose

定義 `nlm_cookie_refresh.py` 排程腳本在每次執行完成後，如何將本次執行結果（成功或失敗）通知管理員，讓這個自動化排程的運作狀況可以被觀察，而不需要手動登入伺服器或打開工作排程器查看。

## ADDED Requirements

### Requirement: 排程執行完成後必須通知管理員結果
`nlm_cookie_refresh.py` 每次執行完成，不論成功或失敗，系統 SHALL 嘗試發送一則 LINE 通知給管理員，摘要本次執行的結果。若管理員告警未設定（`ADMIN_LINE_USER_ID`/`ADMIN_ALERT_ACCESS_TOKEN` 為空），系統 MUST NOT 發送通知，且此情況不得視為錯誤。

#### Scenario: 成功續期所有已綁定 channel
- **WHEN** cookie 讀取成功，且所有已綁定 channel 的 rebind 都成功
- **THEN** 系統發送一則精簡的成功摘要通知給管理員，內容包含本次成功續期的 channel 數量

#### Scenario: 無法從本機瀏覽器讀取到 cookie
- **WHEN** `notebooklm login --browser-cookies` 執行失敗或逾時
- **THEN** 系統發送失敗通知給管理員，說明需要在該機器上手動重新登入，且不嘗試對任何 channel 進行 rebind

#### Scenario: 部分 channel rebind 失敗
- **WHEN** cookie 讀取成功，但一個或多個已綁定 channel 的 rebind 呼叫失敗
- **THEN** 系統發送通知列出失敗的 channel 與各自的錯誤原因；成功的 channel 不受影響，也不會被回報為失敗

#### Scenario: 沒有任何已綁定的 channel
- **WHEN** cookie 讀取成功，但資料庫中沒有任何 `notebook_id` 不為空的 channel
- **THEN** 系統不發送通知，因為沒有任何實際執行結果需要回報

#### Scenario: 管理員告警未設定
- **WHEN** `ADMIN_LINE_USER_ID` 或 `ADMIN_ALERT_ACCESS_TOKEN` 未設定
- **THEN** 系統不嘗試發送任何通知，且此次執行仍視為正常完成
