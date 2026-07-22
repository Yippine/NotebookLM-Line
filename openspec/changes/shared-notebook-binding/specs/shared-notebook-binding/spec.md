## ADDED Requirements

### Requirement: 顯示免安裝分享指引
系統 SHALL 在 NotebookLM 綁定步驟顯示課程專用 Gmail、Viewer 權限要求、資料可見性說明及學員應貼上自己 Notebook 網址的明確指引，且不得要求學員安裝工具或提供 Google Cookie。

#### Scenario: 學員進入 NotebookLM 綁定步驟
- **WHEN** 已授權的學員完成 LINE Channel 設定並進入 NotebookLM 步驟
- **THEN** 系統顯示固定的課程帳號 email、私人分享步驟與 Notebook URL 欄位
- **THEN** 畫面不得顯示 `storage_state.json` 上傳、Cookie 貼上或本機 CLI 安裝要求

#### Scenario: 說明資料可見性
- **WHEN** 學員閱讀分享指引
- **THEN** 系統明確告知課程帳號可讀取被分享 Notebook 的來源與回答
- **THEN** 系統提供取消分享及解除綁定的說明

### Requirement: 驗證並正規化 Notebook URL
系統 SHALL 只接受設定 allowlist 中的官方 HTTPS Notebook 網址，並在保存前正規化及擷取 Notebook ID。

#### Scenario: 提交有效 Notebook 網址
- **WHEN** 已授權學員提交 allowlist host 上且包含有效 Notebook ID 的 HTTPS 網址
- **THEN** 系統擷取正規化 Notebook ID 並進入存取驗證

#### Scenario: 提交非官方或格式錯誤網址
- **WHEN** 學員提交非 HTTPS、非 allowlist host、缺少 Notebook ID 或無法解析的網址
- **THEN** 系統拒絕請求且不修改既有綁定
- **THEN** 系統回傳不含內部例外或敏感資料的繁體中文修正指引

### Requirement: 使用課程帳號驗證共享 Notebook
系統 SHALL 使用課程專用 NotebookLM 帳號確認指定 Notebook 可讀取，並在學員明確執行測試時完成最小聊天測試後才建立綁定。

#### Scenario: Viewer Notebook 驗證成功
- **WHEN** Notebook 已私下分享給課程帳號且 Viewer 可讀取與聊天
- **THEN** 系統取得 Notebook 顯示名稱並完成固定的最小聊天測試
- **THEN** 系統保存 Channel 與 Notebook ID 的對應並回傳 `bound` 狀態

#### Scenario: Notebook 尚未分享
- **WHEN** 課程帳號無權讀取提交的 Notebook ID
- **THEN** 系統不得保存或覆蓋綁定
- **THEN** 系統提示學員確認分享 email 與 Viewer 權限後重新測試

#### Scenario: 課程帳號授權失效
- **WHEN** 驗證過程判定課程帳號登入已失效
- **THEN** 系統不得保存或覆蓋綁定
- **THEN** 系統回傳 `course_account_unavailable`，並提示聯繫管理者而不揭露授權內容

### Requirement: 保存 Channel 與 Notebook 的隔離映射
系統 SHALL 為每個 LINE Channel 保存單一正規化 Notebook ID，且不得在 Channel 記錄中保存學員 Google Cookie。

#### Scenario: 兩位學員綁定不同 Notebook
- **WHEN** Channel A 綁定 Notebook A，Channel B 綁定 Notebook B
- **THEN** Channel A 的提問只送到 Notebook A
- **THEN** Channel B 的提問只送到 Notebook B

#### Scenario: 重新綁定同一 Channel
- **WHEN** Channel 擁有者成功驗證另一個可存取的 Notebook
- **THEN** 系統以單一交易更新 Notebook ID、顯示名稱與檢查時間
- **THEN** 驗證失敗時保留原本可用的綁定

### Requirement: 提供可操作的綁定狀態
系統 SHALL 回報 `unbound`、`checking`、`bound`、`access_revoked`、`course_account_unavailable` 或 `error` 狀態，且不得僅以資料庫欄位是否存在判定可用性。

#### Scenario: 最近驗證成功
- **WHEN** Channel 已保存 Notebook ID 且最近一次即時檢查成功
- **THEN** 狀態 API 回傳 `bound`、Notebook 顯示名稱與最後檢查時間

#### Scenario: 學員撤銷分享
- **WHEN** 即時檢查或實際提問發現課程帳號已無權存取 Notebook
- **THEN** 系統將狀態更新為 `access_revoked`
- **THEN** LINE 與設定頁顯示重新分享並測試的指引

#### Scenario: 上游發生未知錯誤
- **WHEN** NotebookLM 回傳無法分類的錯誤
- **THEN** 系統回傳 `error` 與可供支援追查的 request ID
- **THEN** 系統不得向學員顯示 Cookie、RPC payload 或完整堆疊資訊

### Requirement: 產生適合 LINE 的回答與等待狀態
系統 SHALL 將 NotebookLM 回答轉換為 LINE 可閱讀的純文字，移除無法在 LINE 開啟對應內容的純數字引用，且 MUST NOT 另外傳送只有檔名、沒有可操作連結的來源清單。長時間查詢 SHALL 使用 LINE Loading Animation，並在回答尚未完成時於動畫到期前續期。

#### Scenario: 回答包含群組引用編號
- **WHEN** NotebookLM 回答包含 `[1]`、`[1, 2]` 或其他純數字引用
- **THEN** LINE 收到的回答正文不包含這些引用標記
- **THEN** 系統不另外傳送只有引用編號與來源檔名的訊息

#### Scenario: 查詢超過一分鐘
- **WHEN** NotebookLM 回答在首次 60 秒 Loading Animation 結束前仍未完成
- **THEN** 系統在約 50 秒時再次呼叫 Loading API 並設定 60 秒
- **THEN** 系統持續續期直到回答、錯誤或取消事件準備送出

#### Scenario: Loading API 失敗
- **WHEN** 首次 Loading API 回傳錯誤或發生網路失敗
- **THEN** 系統可使用 Reply Message 傳送簡短文字等待提示
- **THEN** Loading 失敗不得中止 NotebookLM 查詢或最終 Push Message

#### Scenario: 不公開私人 Notebook 網址
- **WHEN** 系統將回答傳送到 LINE 使用者
- **THEN** 系統不得為了取代引用而附上含私人 Notebook ID 的 NotebookLM 網址

### Requirement: 解除綁定與取消分享
系統 SHALL 允許 Channel 擁有者解除 Notebook 綁定，並清楚提醒學員另行在 NotebookLM 取消對課程帳號的分享。

#### Scenario: 成功解除綁定
- **WHEN** 已授權 Channel 擁有者確認解除綁定
- **THEN** 系統清除該 Channel 的 Notebook ID、顯示名稱與存取狀態
- **THEN** 後續 LINE 提問回覆尚未綁定提示

#### Scenario: 解除綁定不影響其他學員
- **WHEN** Channel A 解除綁定
- **THEN** 系統不得修改 Channel B 的 Notebook 映射或狀態

#### Scenario: Channel 因到期遭清除
- **WHEN** 管理員先前為 Channel 設定的個別期限到達且系統完成清理
- **THEN** 系統移除該 Channel 的 Notebook 映射並停止 LINE Bot 回答
- **THEN** 系統不得刪除學員原始 Notebook 或代替學員取消 Google 分享

### Requirement: 遷移既有 Cookie 綁定
系統 SHALL 提供受控過渡流程，讓既有 Channel 在分享並驗證 Notebook 後移除每 Channel 的 NotebookLM 授權資料。

#### Scenario: 既有學員完成新式綁定
- **WHEN** 既有 Channel 以課程帳號成功驗證目前 Notebook
- **THEN** 系統保留 Notebook ID 並清除該 Channel 的 `nlm_auth_json_encrypted`
- **THEN** 後續問答只使用課程帳號授權

#### Scenario: 過渡期尚未完成
- **WHEN** 既有 Channel 尚未分享 Notebook 或新式驗證失敗
- **THEN** 系統標示需要遷移且不得在學員介面重新要求上傳 Cookie
