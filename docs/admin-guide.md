# V2 管理者操作手冊

本文件適用於 `shared-notebook-binding` 新版環境。新版使用一個課程專用的一般 Gmail 讀取學員私下分享的 Notebook，不再收集每位學員的 Google Cookie。

## 角色與帳號

- 系統管理者：使用 `/admin` 與管理員密碼登入，管理邀請碼、LINE Channel 與課程 NotebookLM 帳號。
- 課程 Gmail：一般 Google 帳號，專門接收學員以 Viewer 權限分享的 Notebook。
- 學員：用邀請碼進入 `/setup`，設定自己的 LINE Channel，分享自己的 Notebook，再貼 Notebook URL。

系統管理者和課程 Gmail 是兩個不同角色。管理者可以由同一位講師操作，但請勿使用講師的日常私人 Gmail 作為課程帳號。

## 第一次設定

1. 建立或指定一個只供本服務使用的一般 Gmail。
2. 直接登入 NotebookLM，完成服務條款、復原方式與兩步驟驗證設定。
3. 在受信任的管理電腦執行 `notebooklm login --storage <安全路徑>`，取得課程帳號的 `storage_state.json`。
4. 登入新版管理後台。
5. 在「課程 NotebookLM 帳號」輸入同一個 Gmail，選取授權檔內容並按「驗證並儲存」。
6. 確認健康狀態為 `healthy`，再產生學員邀請碼。

> 一般消費者版 NotebookLM 目前沒有供本流程使用的正式 OAuth／聊天 API。管理後台輸入 Gmail 不會自動取得 NotebookLM 權限；必須另外提供該 Gmail 自己的 NotebookLM 工作階段。授權只應在管理者受信任環境處理。

新版 `notebooklm-py` 授權檔若帶有帳號 metadata，系統會比對輸入 Gmail 並拒絕不一致的授權；舊式純 Cookie 檔可能沒有可驗證 email，此時管理者仍必須人工確認瀏覽器登入帳號與輸入 Gmail 完全相同。這項限制必須納入真實帳號驗收。

## 日常管理

- `healthy`：課程帳號可用，可以接受學員綁定與 LINE 問答。
- `unconfigured`：尚未設定 Gmail 或授權。
- `expired`：Google 工作階段失效，需重新登入並更新授權。
- `error`：可能是暫時性網路、限流或 NotebookLM 服務問題；先按「立即檢查」，不要直接要求所有學員重新分享。

管理後台只顯示帳號 email、健康狀態、最後檢查時間和遮蔽後錯誤碼，不會回顯 Cookie 或 LINE 憑證。

新產生的邀請碼預設有效 90 天（由 `INVITE_CODE_TTL_SECONDS` 設定）。學員可在期限內回家重新輸入同一邀請碼；每次重新登入會撤銷該邀請碼先前簽發的 setup session，只保留最新工作階段。若邀請碼外洩，管理者應刪除對應 Channel／邀請資料並重新產生，不要只等待瀏覽器 token 到期。

手動產生的邀請碼預設沒有姓名。管理者可在「未使用的邀請碼」先按「編輯」填入姓名，也可等學員綁定後，在「學員綁定狀況」新增或修改；姓名只作為管理標籤，不會影響邀請碼、LINE、NotebookLM 綁定或到期時間。

### 課程到期時間

- 每位學員列最左側都有勾選框；表頭左上角勾選框可全選或取消全選目前全部學員。
- 選好日期後按「套用至已勾選學員」，只更新勾選的 Channel，不修改其他學員已有日期。
- 按「取消已勾選到期」只清除勾選學員的到期時間，並保留綁定資料。
- 新加入的學員預設不限期；管理者可只勾選新學員，再套用另一個日期。
- 到期後系統只會永久刪除已套用時間的學員邀請碼、setup session、LINE Channel 與 NotebookLM 綁定，並停止其 LINE Bot 回覆；刪除後無法復原。未套用的新學員及尚未使用的邀請碼不受影響。
- 若要恢復不限期使用，必須在到期前按「取消到期限制」。取消會清除目前所有 Channel 的到期時間，並保留尚未被到期程序刪除的學員與綁定。
- 到期清除不會刪除管理員帳號或課程 Gmail 授權，下一期可以直接重新建立邀請碼。
- 管理後台以台灣時間顯示目前設定。修改日期前請先核對畫面顯示的「目前全部到期時間」。

## 更換課程 Gmail

第一版只支援一個作用中的課程 Gmail。更換帳號會影響所有既有 Notebook：

1. 先不要清除目前有效授權。
2. 準備新課程 Gmail 並完成 NotebookLM 登入。
3. 通知學員把既有 Notebook 分享給新 Gmail。
4. 在維護時段提交新 Gmail 與其授權並完成健康檢查。
5. 系統會保留 Notebook ID，但把原本綁定標為課程帳號暫不可用；學員分享給新 Gmail 後，需逐一按「重新檢查」。
6. 抽查多個 Channel 的 Notebook 綁定與 LINE 問答。

若新授權驗證失敗，系統應保留原授權，不得覆蓋。第一版尚未實作「候選帳號」雙階段切換，因此切換前必須先完成通知與備份。

## 授權失效處理

1. 確認管理後台顯示 `expired`，而不是單一 Notebook 的 `access_revoked`。
2. 在受信任電腦重新執行 `notebooklm login`。
3. 於管理後台提交相同 email 的新授權。
4. 執行健康檢查，再抽查一個自有 Notebook與一個 Viewer shared Notebook。
5. 確認 LINE Loading 動畫與背景 Push 回覆正常。

請勿透過 LINE、Email、即時通訊或工單傳送 `storage_state.json`。
