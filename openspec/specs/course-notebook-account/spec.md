## Purpose

定義課程專用一般 Gmail 的集中授權、健康檢查、持久續期、重新驗證及 NotebookLM 查詢資源控制。

## Requirements

### Requirement: 管理課程專用一般 Gmail
系統 SHALL 讓管理者設定一個專供課程 NotebookLM 整合使用的一般 Gmail，並明確標示它不是 Google Workspace 或 Google Cloud Service Account。

#### Scenario: 管理者完成初始設定
- **WHEN** 已授權管理者提交課程帳號 email 與有效 NotebookLM 授權
- **THEN** 系統驗證帳號可使用 NotebookLM
- **THEN** 系統保存 email、加密授權、健康狀態與檢查時間

#### Scenario: 學員取得分享目標
- **WHEN** 已授權學員進入 NotebookLM 綁定頁
- **THEN** 系統只回傳課程帳號 email 與非敏感健康狀態
- **THEN** 系統不得回傳任何課程帳號授權、Cookie、master token 或復原資訊

### Requirement: 加密並集中保存 NotebookLM 授權
系統 MUST 只在伺服器端加密保存一份課程 NotebookLM 授權，所有 Channel MUST 共用該授權且不得再保存個別學員 Google 工作階段。

#### Scenario: 服務處理 NotebookLM 查詢
- **WHEN** 任一已綁定 Channel 發出問題
- **THEN** 系統在伺服器端解密課程帳號授權並建立 NotebookLM client
- **THEN** 系統使用該 Channel 的 Notebook ID 執行查詢

#### Scenario: 正式環境缺少固定加密金鑰
- **WHEN** 正式環境啟動時未提供有效固定加密金鑰
- **THEN** 系統拒絕啟動並記錄不含秘密值的設定錯誤
- **THEN** 系統不得自動產生只在該程序有效的臨時金鑰

### Requirement: 提供課程帳號健康檢查
系統 SHALL 定期及按需檢查課程帳號授權，並維護 `unconfigured`、`healthy`、`expired` 或 `error` 健康狀態。

#### Scenario: 健康檢查成功
- **WHEN** 課程帳號可以建立 client 並完成非破壞性 NotebookLM 存取
- **THEN** 系統將狀態設為 `healthy` 並更新最後成功時間

#### Scenario: 授權過期
- **WHEN** Google／NotebookLM 明確拒絕目前授權
- **THEN** 系統將狀態設為 `expired`
- **THEN** 管理後台顯示需要重新驗證，學員端只顯示暫時無法連線

#### Scenario: 暫時性上游錯誤
- **WHEN** 健康檢查遇到逾時、限流或暫時性伺服器錯誤
- **THEN** 系統將狀態設為 `error` 或保留最近健康狀態並記錄錯誤時間
- **THEN** 系統不得把暫時性錯誤誤判為所有 Notebook 已撤銷分享

### Requirement: 持久保存 NotebookLM 輪替授權
系統 MUST 將加密資料庫中的課程帳號 `storage_state` 視為唯一授權來源，並在一般查詢與至少每 15 分鐘一次的健康檢查完成後，安全保存 NotebookLM client 產生的有效 Cookie 輪替。

#### Scenario: 成功操作產生新的 Cookie 狀態
- **WHEN** NotebookLM 操作成功，且 client 使用的 storage state 已發生實質變更
- **THEN** 系統在刪除短效臨時檔前驗證更新後的 JSON 與必要 Cookie
- **THEN** 系統重新加密授權，使用原始 `auth_revision` 比較並交換寫回，成功後遞增 revision

#### Scenario: 授權內容沒有變更
- **WHEN** NotebookLM 操作完成，但更新後的 storage state 與原資料語意相同
- **THEN** 系統不建立不必要的資料庫 revision
- **THEN** 系統仍刪除包含明文授權的短效臨時檔

#### Scenario: 並行操作遇到較新授權版本
- **WHEN** client 準備寫回時，資料庫 `auth_revision` 已不是該 client 建立時讀取的版本
- **THEN** 系統不得用舊候選授權覆蓋較新的加密授權
- **THEN** 系統捨棄舊候選值，且下一次操作重新載入最新 revision

#### Scenario: 回答成功但授權寫回失敗
- **WHEN** NotebookLM 已產生有效回答，但更新後的授權無法驗證、加密或寫回
- **THEN** 系統可交付本次回答，但保存不含秘密值的 `auth_persistence_failed` 狀態並通知維運者
- **THEN** 系統不得將 Cookie、storage state 或解密內容寫入 log 或回傳給使用者

#### Scenario: 服務重新啟動
- **WHEN** backend 或容器在成功保存輪替授權後重新啟動
- **THEN** 新 client 使用資料庫中最新版加密 storage state
- **THEN** 系統不依賴已刪除的臨時檔或程序記憶體恢復登入

### Requirement: 正確分類課程帳號授權失效
系統 MUST 將明確的 NotebookLM 認證失效與暫時性上游錯誤分開處理，避免真正過期被誤標為一般錯誤，也避免短暫網路問題要求管理者重新登入。

#### Scenario: 套件回報明確認證失效
- **WHEN** `notebooklm-py` 回報 `Authentication expired or invalid`、HTTP 401 或等價的登入失效訊號
- **THEN** 系統將課程帳號標記為 `expired`，並保存遮蔽後錯誤碼 `course_auth_expired`
- **THEN** 管理後台顯示重新驗證操作，學員端不得看到原始例外或授權內容

#### Scenario: 發生暫時性連線或服務錯誤
- **WHEN** NotebookLM 操作遇到逾時、限流或暫時性 5xx 錯誤，且沒有明確認證失效訊號
- **THEN** 系統將事件分類為暫時性 `error`，不得將課程帳號標記為 `expired`
- **THEN** 系統不得覆蓋目前加密授權或要求管理者立即重新登入

### Requirement: 管理員安全重新驗證
系統 SHALL 提供僅管理員可用的重新驗證流程，成功後原子替換課程帳號授權並立即執行健康檢查。

#### Scenario: 重新驗證成功
- **WHEN** 管理者提交有效的新授權
- **THEN** 系統先驗證新授權，再原子替換舊授權
- **THEN** 既有 Channel 與 Notebook ID 映射維持不變

#### Scenario: 重新驗證失敗
- **WHEN** 新授權無效或無法通過健康檢查
- **THEN** 系統不得覆蓋原有授權
- **THEN** 系統回傳遮蔽後的錯誤與 request ID

### Requirement: 控制集中帳號的並發與用量
系統 SHALL 使用可設定的並發上限控制課程帳號查詢，並記錄不含問題內容與憑證的成功、失敗、延遲及限流指標。

#### Scenario: 查詢量低於上限
- **WHEN** 同時執行中的 NotebookLM 查詢少於設定上限
- **THEN** 系統允許查詢執行並記錄耗時與結果類別

#### Scenario: 查詢量達到上限
- **WHEN** 同時查詢數已達設定上限
- **THEN** 系統將新工作排入有界佇列或回覆稍後再試
- **THEN** 系統不得無限制建立背景工作或讓不同 Channel 共用錯誤的 Notebook context

### Requirement: 鎖定並驗證 NotebookLM 相依版本
系統 MUST 使用經 Viewer shared Notebook PoC 通過的固定 `notebooklm-py` 版本，升級前 MUST 通過相容性測試。

#### Scenario: 建置正式版本
- **WHEN** 安裝後端相依套件
- **THEN** 套件管理器安裝規格中鎖定的 `notebooklm-py` 版本，而不是不受限制的最新版

#### Scenario: 升級 NotebookLM 套件
- **WHEN** 維護者提議更新 `notebooklm-py`
- **THEN** 自有 Notebook、Viewer shared Notebook、聊天、引用來源、撤銷分享與慢速回覆測試全部通過後才能部署
