## Context（背景）

目前系統讓每位學員建立自己的 LINE Messaging API Channel，透過邀請碼進入設定頁，填入 Channel ID、Channel Secret 與 Channel Access Token；後端為每個 Channel 建立專屬 Webhook，收到文字訊息後呼叫 `notebooklm-py`，再以 LINE Push Message 回傳回答。

現行 NotebookLM 綁定要求每位學員提供 `storage_state.json`。後端把每份 Google 工作階段加密存入 `channels.nlm_auth_json_encrypted`，並在每次查詢時以全域環境變數 `NOTEBOOKLM_AUTH_JSON` 切換帳號。這造成以下問題：

- 學員必須安裝工具並處理各種電腦環境差異。
- 系統收集每位學員的 Google Cookie，風險與維護成本高。
- 全域環境變數必須以程序鎖保護，使不同學員的查詢被序列化。
- NotebookLM、狀態與選擇端點沒有一致驗證設定階段的學員身分。
- 邀請碼工作階段只存在記憶體，重新啟動後即失效；部分敏感資料也出現在 query string 或以明文保存。

新的架構改由一個課程專用的一般 Gmail 集中存取所有學員私下分享的 Notebook。學員僅提供自己的 Notebook 網址，系統保存 Channel 與 Notebook ID 的映射。NotebookLM 目前沒有適合此消費者產品流程的正式公開聊天 API，因此仍以 `notebooklm-py` 整合，並接受非官方介面可能變更的營運限制。

利害關係人包括學員、講師／管理者、系統維運者，以及透過學員 LINE 官方帳號提問的使用者。

## Goals / Non-Goals（目標與非目標）

**Goals（目標）：**

- 讓學員只用瀏覽器即可完成 LINE Channel 與自己 NotebookLM 的綁定。
- 不再要求學員安裝工具、上傳 Cookie 或提供 Google 密碼。
- 由管理者維護單一課程 NotebookLM 帳號的伺服器端授權。
- 在綁定時驗證 Notebook URL、存取權與實際聊天能力。
- 確保每個 LINE Channel 只查詢其綁定的 Notebook。
- 提供可理解的連線狀態、撤銷分享、帳號失效與重新驗證流程。
- 讓管理者能辨識手動產生邀請碼的學員，並針對勾選的學員個別或批次設定使用期限。
- 到期後清除該學員在服務內的綁定與憑證，使 LINE Bot 不再回答，同時隔離其他未到期學員。
- 修正設定 API、敏感資料保存與瀏覽器來源限制等必要安全缺口。
- 保留現有 LINE Loading 動畫與背景 Push Message 回覆架構，支援共享 Notebook 較長的回應時間。

**Non-Goals（非目標）：**

- 不建立 Google Workspace、Google Cloud Service Account 或正式 NotebookLM OAuth 整合。
- 不讓系統代替學員建立、編輯或刪除 Notebook 及來源。
- 不支援公開連結作為主要授權方式。
- 第一版不支援多個課程帳號、跨班級帳號池或自動負載分流。
- 第一版不驗證 Notebook 的法律所有權；持有不可猜測的 Notebook URL、通過學員設定權杖且該 Notebook 已分享給課程帳號，視為具有綁定權限。
- 不在本變更中遷移到 Gemini File Search、Dify、Flowise 或其他 RAG 平台。
- 不保證非官方 NotebookLM 介面永遠相容；本變更提供版本鎖定、健康檢查與失效處理。

## Decisions（技術決策）

### 1. 採用單一課程專用一般 Gmail

管理者建立一個只供本系統使用的一般 Gmail。每位學員把自己的 Notebook 私下分享給該帳號，權限設定為 Viewer。後端以這個帳號查詢所有已分享 Notebook。

選擇原因：

- 學員不需安裝任何工具，也不交付 Google Cookie。
- 系統只維護一份 NotebookLM 授權。
- 現有資料模型只需從「每 Channel 一份授權」改成「全站一份授權＋每 Channel 一個 Notebook ID」。
- `notebooklm-py` 已有真實共享 Notebook 的聊天案例，適合先進行小型 PoC。

替代方案：

- 每位學員繼續上傳 Cookie：拒絕，因為操作與安全問題正是本變更要解決的核心。
- Chrome Cookie 擴充功能：僅能作為暫時備援，仍需安裝且會交付敏感 Cookie。
- NotebookLM MCP Server：仍需要 Chrome／Cookie，並新增 Node、瀏覽器與 MCP 服務層，對現有 FastAPI 架構更複雜。
- Flowise／Dify／Gemini File Search：需要重新匯入原始來源，無法直接沿用學員已建立的 Notebook，留待長期架構評估。

### 2. 集中保存課程帳號授權，不再保存學員授權

新增單例 `course_notebook_account` 資料，保存課程帳號 email、加密授權資料、健康狀態、最後檢查時間與非敏感錯誤碼。授權資料使用固定的 Fernet 金鑰加密；正式環境未設定金鑰時應拒絕啟動，而不是產生臨時金鑰。

第一版明確採用經加密的 `storage_state` 作為授權來源，不採用 durable master token。`course_notebook_account.auth_encrypted` 是唯一真實來源；伺服器每次建立 NotebookLM client 時，讀取當下 `auth_revision`，解密至權限為 `0600` 的短效臨時檔。操作結束後必須在刪除臨時檔前重新讀取其內容，驗證 JSON 結構與必要 Cookie，若內容有實質變更，便重新加密並以 `WHERE auth_revision = <原版本>` 的比較並交換方式寫回，再遞增 revision。

一般查詢與每 15 分鐘執行的健康檢查都必須保存 `notebooklm-py` 產生的 Cookie 輪替，使帳號即使沒有學員提問仍可持續刷新。臨時檔不屬於持久授權儲存，無論成功或失敗都必須刪除；Cookie、完整 storage state 與解密內容不得寫入 log、錯誤回應或監控標籤。

若並行操作期間已有較新的 revision，舊 client 的候選授權不得覆蓋較新資料；系統捨棄舊候選值，下一次操作重新載入最新版。單純發生 revision 衝突時，不得為了寫回而重送已完成的 NotebookLM 問答。若回答已成功但授權寫回失敗，仍可交付該次回答，但必須記錄不含秘密值的 `auth_persistence_failed`、更新管理健康狀態並告警；健康檢查本身的寫回失敗則視為檢查失敗。

管理者重新驗證仍採「先驗證、後原子替換」：新授權通過實際 NotebookLM 探測後才遞增 revision 並取代舊資料。Durable master token 因權限更高且尚未完成安全及部署驗證，延後至獨立變更評估，不作為第一版失效補救方式。

NotebookLM 查詢服務直接讀取集中授權，不再以每個 Channel 的資料覆寫程序環境變數。每個請求建立受控的 client context，並使用可設定的 semaphore 限制同時查詢數，避免超出帳號配額或造成服務不穩。

### 3. 以 Notebook URL 綁定，保存正規化 Notebook ID

新增綁定 API，輸入包含 Notebook URL。後端必須：

1. 只接受 HTTPS 與設定的官方 NotebookLM host allowlist。
2. 從網址正規化並擷取 Notebook ID。
3. 使用課程帳號呼叫 `notebooks.get(notebook_id)` 驗證讀取權。
4. 在學員明確按下「測試並綁定」時送出固定、最小的聊天測試，以確認 Viewer Notebook 可回答；畫面需告知此動作會產生一次測試查詢。
5. 成功後才以同一交易保存 `notebook_id`、顯示名稱、`bound_at` 與 `last_access_checked_at`。

系統不依賴「列出所有 Notebook 再讓學員選擇」，因為共享 Notebook 可能延遲出現在清單中，而學員已提供精確網址。既有 Notebook 選擇 API 將改為只接受目前設定工作階段授權，且必須再次驗證課程帳號可存取目標 ID。

### 4. 綁定狀態區分資料存在與即時可用性

狀態不再以「資料庫中是否有加密 Cookie」判斷。Channel 綁定狀態至少包含：

- `unbound`：尚未保存 Notebook ID。
- `checking`：正在驗證。
- `bound`：最近一次存取檢查成功。
- `access_revoked`：Notebook 不存在、已刪除或不再分享給課程帳號。
- `course_account_unavailable`：課程帳號授權已過期或服務不可用。
- `error`：其他已遮蔽的錯誤。

設定頁取得狀態時可使用短時間快取；使用者按「重新檢查」或實際提問失敗時必須做即時檢查。不得把上游 Cookie、RPC payload 或完整例外訊息傳回前端。

### 5. 以持久化、具範圍的 Bearer 工作階段保護設定 API

邀請碼驗證成功後，系統產生高熵 opaque token，只將雜湊與過期時間保存於 `setup_sessions`。前端在 `Authorization: Bearer <token>` 傳送，不再放在 query string。

每個設定工作階段綁定一個邀請碼及其 Channel；所有 Channel 建立、查詢、Notebook 綁定、狀態、重新檢查、切換及解除綁定操作都必須驗證 token 有效、未過期且擁有該 Channel。

管理員密碼只允許透過 HTTPS request body 交換短效管理員工作階段；後續管理 API 使用 Bearer token，不再把密碼放入網址。工作階段重啟後仍可依資料庫驗證，並支援撤銷與到期。

### 6. 敏感資料加密、來源限制與安全紀錄

- LINE Channel Secret、Channel Access Token 與課程 NotebookLM 授權必須加密保存。
- Webhook 執行時才解密 LINE 憑證，且不得寫入 log。
- CORS 只允許設定中的正式前端來源；LINE Webhook 不依賴瀏覽器 CORS，仍維持公開 HTTPS endpoint 與簽章驗證。
- 綁定與登入端點依工作階段及來源 IP 做速率限制。
- Log 只記錄 request ID、遮蔽後 Channel ID、結果碼與耗時；不得記錄邀請碼、Bearer token、管理員密碼、LINE 憑證、Google Cookie 或完整 Notebook URL。

### 7. 保留 LINE 非同步回覆模式

共享 Notebook 的回答可能需要數十秒。Webhook 必須快速回傳 2xx，顯示 LINE Loading 動畫，並在背景完成 NotebookLM 查詢後以 Push Message 回傳。此設計避免依賴一次性、期限短的 Reply Token。

Loading Animation 必須呼叫官方 `POST /v2/bot/chat/loading/start` 並將 `202` 視為成功。第一次設定 `loadingSeconds: 60`；若回答在 50 秒後仍未完成，重複呼叫同一使用者的 Loading API，以新的 60 秒覆蓋剩餘時間，直到回答、錯誤訊息或取消事件準備送出。只有第一次 Loading API 明確失敗時才以 Reply Message 傳送文字等待提示。動畫僅作為一對一手機聊天室的視覺增強，不得影響背景查詢、持久化重試或最終 Push 的可靠性。

NotebookLM 回答中的 `[1]`、`[1, 2]` 等數字引用在 LINE 無法連回對應段落，來源檔名也不一定具有可開啟網址。因此 LINE formatter 必須移除純數字引用，且第一版不額外傳送來源清單或 Notebook URL。此決策同時避免洩漏私人 Notebook ID，並省略不必要的來源清單查詢。若未來上游能穩定提供公開 HTTPS 來源，可另行設計只在同一則回答底部附加可操作連結的功能。

由於 LINE 收到 2xx 後不應被當作背景失敗的重送來源，服務在回傳 2xx 前必須先將必要的待處理內容以固定 Fernet 金鑰加密寫入有界的持久化工作帳本。密文只包含版本、Channel、LINE 使用者與問題，不包含 Reply Token 或 Access Token；每次執行都從 Channel 資料重新取得加密憑證。工作只在回答完整 Push 後標記完成並清除密文；暫時失敗由內部排程器以退避有限次重試，服務重啟時可恢復。成功、不可重試、達次數上限或超過保留期時必須清除密文；V2 預設 pending retention 為 24 小時，且不將問題內容寫入 log。

工作帳本必須同時有全站與每 Channel 上限，Webhook 也必須限制 body 大小與單次事件數；重播排程以 Channel 公平分批，避免單一學員耗盡全站容量。每個 durable event 的各次 Push 使用從 event ID 與訊息順序決定性產生的 LINE retry-key UUID，使「Push 已被 LINE 接受、但本地完成記錄尚未寫入」的崩潰恢復不會重複投遞。Pending retention 不得超過 LINE retry key 的 24 小時有效期。

V2 第一版的 SQLite outbox 與 Notebook 對話鎖以單一 backend process／replica 為運行前提。水平擴充前必須改用外部工作佇列與分散式鎖，不得直接增加 Uvicorn workers。

若 Notebook 存取已撤銷或課程帳號失效，背景工作回傳簡短使用者訊息，並更新健康狀態；技術細節只進入已遮蔽的管理紀錄。

### 8. 鎖定 `notebooklm-py` 版本並建立相容性測試

`backend/requirements.txt` 必須鎖定 PoC 通過的精確版本或不可變 commit。升級前必須執行：

- 課程帳號授權健康檢查。
- 自有 Notebook 與 Viewer shared Notebook 的 `get`、`chat.ask`、引用來源測試。
- 多 Notebook 路由與撤銷分享測試。
- 慢速回答下的 LINE 背景 Push 測試。

### 9. 以學員勾選清單管理姓名與個別到期時間

管理後台的「學員綁定狀況」以 Channel 為管理單位。每列提供勾選框、學員姓名、邀請碼、Channel ID、NotebookLM 狀態、到期時間及操作；表頭勾選框只全選目前清單中的學員。管理者選擇日期後，系統只更新本次勾選的 Channel，因此不同學員可以保有不同到期日。

新建立或新綁定的 Channel 預設不設到期時間，不得自動繼承上一次批次操作或其他學員的日期。管理者也可只對勾選的 Channel 取消期限，取消期限只清空到期欄位，不解除 LINE 或 NotebookLM 綁定。`course_settings.course_expires_at` 若保留，只能作為管理畫面最近一次選擇日期的提示，不得作為新 Channel 的期限來源。

手動產生的邀請碼允許姓名為空。管理者可在未使用邀請碼及已綁定學員清單補填、修改或清除姓名；修改只更新邀請碼的 `student_name`，不得更換邀請碼、Channel、Notebook、設定工作階段或到期時間。

到期清理由後端排程以每個 Channel 的 `expires_at` 判斷，並在同一交易中刪除該 Channel 關聯的設定工作階段、已使用邀請碼、Notebook 映射、LINE 憑證及尚未完成的工作帳本事件。清理不得影響其他未到期 Channel 或尚未使用的邀請碼。已被清理的 Channel 若收到 LINE Webhook，系統回傳 2xx 但不啟動查詢或 Push，避免 LINE 重試；Google NotebookLM 中的 Notebook 與分享權限仍由學員自行管理。

選擇原因：

- 到期時間屬於學員／Channel，而不是會自動套用未來學員的全課程全域值。
- 勾選後批次設定同時支援單人、部分與全選，避免另做三套操作。
- 保留管理者補填姓名，可讓手動邀請碼在綁定後仍能辨識。
- 到期刪除本服務資料可立即停止 LINE Bot 使用，也不越權操作學員的 Google 資料。

## Risks / Trade-offs（風險與取捨）

- **[NotebookLM 使用非官方 API，可能無預警改版]** → 鎖定版本、建立 smoke test、提供健康告警與可回滾部署。
- **[課程帳號成為單點故障]** → 定期健康檢查、管理員重新驗證、清楚的全站服務狀態與備份還原程序。
- **[Cookie 已由上游輪替，但新狀態未成功保存]** → 一般查詢與健康檢查皆在刪除臨時檔前驗證並加密寫回；以 revision/CAS 防止舊狀態覆蓋新狀態，寫回失敗時產生安全告警。
- **[所有查詢可能集中計入課程帳號配額]** → 設定同時查詢上限、記錄非敏感用量指標、在擴班前實測容量；多帳號分流不納入第一版。
- **[Viewer shared Notebook 回答較慢]** → 沿用 Loading 動畫、背景任務與 Push Message，不等待 Reply Token。
- **[課程帳號能讀取學員分享的來源]** → 設定頁明確告知資料可見性、只要求 Viewer、提供解除綁定與取消分享說明。
- **[學員可能綁定其他已分享 Notebook 的網址]** → 第一版依賴高熵 URL 與設定權杖；若威脅模型提高，再加入暫時修改 Notebook 標題或擁有者 email 比對的所有權挑戰。
- **[Master token 是高權限長期憑證]** → 第一版不採用；若未來另行評估，必須使用專用帳號、加密保存、限制管理員存取並完成獨立安全審查。
- **[集中帳號查詢的對話可能互相影響]** → 每次新提問使用獨立或明確的 conversation context，不依賴帳號的最後一次對話；至少以 Channel 與 LINE user ID 隔離對話識別。
- **[既有學員重新綁定造成中斷]** → 使用功能旗標與過渡期，先讓既有 Channel 分享 Notebook 並驗證，再停用舊 Cookie 路徑。

## Migration Plan（遷移計畫）

1. 建立一個課程專用一般 Gmail，完成 NotebookLM 首次使用與復原／兩步驟驗證設定。
2. 以兩個測試學員帳號執行 PoC：分享不同 Notebook、以 Viewer 問答、撤銷分享、並發查詢及慢速回覆。
3. 鎖定通過 PoC 的 `notebooklm-py` 版本，新增集中授權、授權 revision、健康檢查與安全設定工作階段資料表。
4. 實作短效 storage-state 臨時檔的 Cookie 輪替讀回、驗證、加密及 revision/CAS 寫回；以一般查詢、15 分鐘健康刷新、並行操作與容器重啟驗證授權可持續使用。
5. 在功能旗標後新增 URL 綁定 API 與新版設定頁；舊 JSON 端點停止出現在學員 UI，但暫時保留為管理員限定的回滾工具。
6. 通知既有學員將 Notebook 分享給課程帳號並貼上網址；成功後保留 Notebook ID、清除該 Channel 的舊授權資料。
7. 所有既有 Channel 完成遷移或過渡期結束後，停用並移除公開 JSON／本機綁定端點。
8. 加密遷移既有 LINE Secret 與 Access Token，確認 Webhook 正常後再清除明文欄位或重建資料表。
9. 啟用 CORS allowlist、Bearer token、速率限制、健康告警及遮蔽紀錄，完成端到端回歸測試後全面切換。
10. 啟用管理者學員勾選、姓名編輯及個別到期時間；先驗證不同日期共存、後加入學員無期限、選取取消期限及到期隔離清理。

回滾時關閉新功能旗標、恢復前一版應用程式並還原遷移前資料庫備份。過渡期內只有尚未清除舊授權的 Channel 可回到舊流程；已清除的學員 Cookie 不得從其他來源重新收集，應要求重新分享或由管理者處理。

## Open Questions（待 PoC 確認）

- 已決定第一版使用「加密資料庫 `storage_state`＋短效臨時檔＋revision/CAS 寫回」；durable master token 不在本變更範圍。
- Viewer shared Notebook 的平均與最長聊天延遲、同時查詢上限及實際配額歸屬。
- 課程帳號是否必須手動開啟分享邀請，或可直接以精確 Notebook ID 存取。
- 目前及重新品牌後的官方 Notebook URL host／path 格式，應納入 allowlist 的精確範圍。
