## ADDED Requirements

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
