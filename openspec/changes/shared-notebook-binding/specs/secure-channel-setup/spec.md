## ADDED Requirements

### Requirement: 使用具範圍的設定工作階段
系統 SHALL 在邀請碼驗證成功後簽發高熵、短效且可撤銷的 opaque setup token，並將其雜湊、到期時間、邀請碼與 Channel 範圍持久化保存。

#### Scenario: 學員成功驗證邀請碼
- **WHEN** 學員提交有效且未過期的邀請碼
- **THEN** 系統回傳一次性的明文 setup token
- **THEN** 資料庫只保存 token 雜湊、有效期限與授權範圍

#### Scenario: 應用程式重新啟動
- **WHEN** 後端重新啟動且學員持有尚未過期、未撤銷的 setup token
- **THEN** 系統仍可從持久化資料驗證該工作階段

#### Scenario: Token 過期或遭撤銷
- **WHEN** 學員使用已過期、未知或遭撤銷的 setup token
- **THEN** 系統回傳 401 並要求重新驗證邀請碼
- **THEN** 系統不得執行任何 Channel 或 Notebook 變更

### Requirement: 以 Authorization Header 傳遞權杖
前端 MUST 以 `Authorization: Bearer` Header 傳送 setup token 或管理員工作階段，且系統 MUST 拒絕以 query string 傳送秘密值的舊式呼叫。

#### Scenario: 合法 Bearer Token
- **WHEN** 前端以 Authorization Header 呼叫受保護 API
- **THEN** 系統驗證 token、期限與資源範圍後才處理請求

#### Scenario: URL 含有 token 或管理員密碼
- **WHEN** 請求把 setup token、管理員密碼或管理員工作階段放在 query string
- **THEN** 系統拒絕該授權方式
- **THEN** 存取紀錄不得保存該秘密值

### Requirement: 強制 Channel 所有權範圍
系統 SHALL 對 Channel 建立、查詢、Notebook 綁定、狀態、重新檢查、切換及解除綁定操作驗證目前 setup token 是否擁有目標 Channel。

#### Scenario: 學員操作自己的 Channel
- **WHEN** setup token 綁定的邀請碼擁有請求中的 Channel ID
- **THEN** 系統允許符合權限的設定操作

#### Scenario: 學員操作其他 Channel
- **WHEN** setup token 不擁有請求中的 Channel ID
- **THEN** 系統回傳 403 且不得洩漏該 Channel 是否存在或其 Notebook 資訊

### Requirement: 管理員使用短效工作階段
系統 SHALL 只透過 HTTPS request body 驗證管理員密碼，成功後簽發短效且可撤銷的管理員 Bearer token，後續管理 API 不得重複接收密碼。

#### Scenario: 管理員登入成功
- **WHEN** 管理員透過登入端點提交正確密碼
- **THEN** 系統回傳短效管理員 token 並只保存其雜湊與期限

#### Scenario: 管理員登入失敗
- **WHEN** 提交錯誤密碼或超過速率限制
- **THEN** 系統回傳通用錯誤且不得指出密碼的部分內容
- **THEN** 系統記錄不含密碼的失敗事件

### Requirement: 加密保存 LINE 與 NotebookLM 秘密
系統 MUST 使用固定、外部提供的加密金鑰保存 LINE Channel Secret、LINE Channel Access Token 與課程 NotebookLM 授權，且解密後的內容 MUST 只存在於執行必要操作的最短生命週期。

#### Scenario: 保存 LINE Channel 憑證
- **WHEN** 已授權學員建立或更新 LINE Channel
- **THEN** 系統驗證輸入後加密保存 Secret 與 Access Token
- **THEN** 一般查詢 API 不得回傳明文憑證

#### Scenario: 處理 LINE Webhook
- **WHEN** LINE 傳送簽章有效的 Webhook
- **THEN** 系統只在驗證簽章及呼叫 LINE API 所需期間解密憑證
- **THEN** 系統不得把明文憑證寫入 log 或錯誤回應

### Requirement: 限制瀏覽器來源並保留 Webhook 驗證
系統 SHALL 將 CORS 限制為設定的正式前端 HTTPS origin，且所有 LINE Webhook MUST 維持 Channel Secret 簽章驗證。

#### Scenario: 正式前端呼叫 API
- **WHEN** 請求來源符合 CORS allowlist
- **THEN** 系統回傳適當的 CORS Header

#### Scenario: 未授權網站呼叫設定 API
- **WHEN** 瀏覽器 Origin 不在 allowlist
- **THEN** 系統不得授予跨來源存取

#### Scenario: LINE Webhook 簽章錯誤
- **WHEN** Webhook 缺少有效 `X-Line-Signature`
- **THEN** 系統拒絕事件且不得執行 NotebookLM 查詢或 LINE Push

### Requirement: 遮蔽敏感紀錄與錯誤
系統 MUST 從應用程式與存取紀錄移除邀請碼、Bearer token、管理員密碼、LINE 憑證、Google Cookie、master token 與完整 Notebook URL。

#### Scenario: 上游回傳含敏感資訊的例外
- **WHEN** Google、NotebookLM 或 LINE client 產生可能包含秘密值的例外
- **THEN** 系統只記錄錯誤分類、遮蔽資源識別碼、request ID 與耗時
- **THEN** 前端只收到繁體中文通用錯誤與 request ID

#### Scenario: 正常設定操作
- **WHEN** 學員或管理員完成受保護操作
- **THEN** 稽核紀錄包含操作類別、時間、結果與遮蔽後識別碼
- **THEN** 稽核紀錄不得包含 request body 中的秘密欄位

### Requirement: 限制高風險端點的呼叫頻率
系統 SHALL 對邀請碼驗證、管理員登入、LINE Channel 更新、Notebook 綁定與課程帳號重新驗證套用可設定的速率限制。

#### Scenario: 正常操作頻率
- **WHEN** 呼叫頻率低於設定門檻
- **THEN** 系統正常處理請求

#### Scenario: 超過速率限制
- **WHEN** 同一工作階段或來源 IP 在時間窗內超過門檻
- **THEN** 系統回傳 429 與可重試時間
- **THEN** 系統不得執行昂貴的 NotebookLM 驗證或登入操作

### Requirement: 管理員可維護邀請碼的學員姓名
系統 SHALL 允許已授權管理員替未使用或已綁定的邀請碼補填、修改或清除學員姓名，且姓名只作為管理辨識標籤。

#### Scenario: 替手動邀請碼補填姓名
- **WHEN** 管理員替原本姓名為空的邀請碼輸入有效姓名
- **THEN** 系統更新該邀請碼的學員姓名並在邀請碼與學員清單顯示
- **THEN** 系統不得更換邀請碼或修改其 Channel 與 Notebook 綁定

#### Scenario: 清除已綁定學員姓名
- **WHEN** 管理員將已綁定學員的姓名清空並儲存
- **THEN** 系統將姓名恢復為空白顯示
- **THEN** 該學員的 setup token、Channel、Notebook 綁定與到期時間保持不變

### Requirement: 管理員以勾選清單設定個別到期時間
系統 SHALL 讓管理員勾選單一、複數或目前清單中的全部學員，並只對勾選 Channel 設定或取消個別到期時間。新建立或新綁定的 Channel MUST 預設為無期限，且 MUST NOT 自動繼承先前批次設定日期。

#### Scenario: 部分學員套用新日期
- **WHEN** 三位學員中只有一位被勾選，且管理員套用新的到期時間
- **THEN** 系統只更新該學員 Channel 的到期時間
- **THEN** 另外兩位學員原有到期時間保持不變

#### Scenario: 使用表頭全選
- **WHEN** 管理員勾選表頭選取框並套用到期時間
- **THEN** 系統更新當下清單中所有被選取學員
- **THEN** 操作完成後才新增或綁定的學員不受此次設定影響

#### Scenario: 取消選取學員的期限
- **WHEN** 管理員勾選部分學員並執行取消期限
- **THEN** 系統只清空被選取 Channel 的到期時間
- **THEN** 系統保留其邀請碼、LINE Channel 與 Notebook 綁定

#### Scenario: 顯示不同學員的日期
- **WHEN** 不同 Channel 具有不同到期時間或其中一個沒有期限
- **THEN** 學員清單逐列顯示各自的到期時間或無期限狀態

### Requirement: 到期時隔離清除該學員服務資料
系統 SHALL 依每個 Channel 的到期時間清除已到期學員在本服務內的設定工作階段、已使用邀請碼、LINE Channel、Notebook 映射與待處理事件，且 MUST NOT 刪除其他學員或 Google NotebookLM 內的資料。

#### Scenario: 單一學員到期
- **WHEN** Channel A 已到期而 Channel B 尚未到期
- **THEN** 系統永久刪除 Channel A 及其關聯服務資料
- **THEN** Channel B 的邀請碼、憑證、Notebook 映射與到期時間保持不變

#### Scenario: 到期 Channel 再收到 Webhook
- **WHEN** 已清除的 Channel 收到 LINE Webhook
- **THEN** 系統回傳成功的 2xx 回應以避免重送
- **THEN** 系統不得執行 NotebookLM 查詢、Loading 動畫或 LINE Push

#### Scenario: 清理後的 Google Notebook
- **WHEN** 系統完成到期清理
- **THEN** 學員 Google 帳號中的 Notebook 與對課程 Gmail 的分享設定保持不變
- **THEN** 系統提示該分享須由學員或管理者在 NotebookLM 另行取消
