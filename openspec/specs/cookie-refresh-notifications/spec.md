# cookie-refresh-notifications Specification

## Purpose

定義 `nlm_cookie_refresh.py` 排程腳本在續期失敗時，如何通知管理員，讓管理員不需要等學員反映、也不需要手動登入伺服器查看，就能及早知道自動續期已經失效。

## Requirements

### Requirement: 續期失敗時必須通知管理員
`nlm_cookie_refresh.py` 只有在本次執行未能成功完成 cookie 續期時，系統 SHALL 發送一則 LINE 通知給管理員，說明失敗原因。成功時系統 MUST NOT 發送任何通知，維持正常運作時完全靜默。若管理員告警未設定（`ADMIN_LINE_USER_ID`/`ADMIN_ALERT_ACCESS_TOKEN` 為空），系統 MUST NOT 發送通知，且此情況不得視為錯誤。

#### Scenario: 成功續期所有已綁定 channel
- **WHEN** cookie 讀取成功，且所有已綁定 channel 的 rebind 都成功
- **THEN** 系統不發送任何通知

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
