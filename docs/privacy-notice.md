# NotebookLM 分享與隱私告知

## 系統會保存

- 學員設定的 LINE Channel ID。
- 加密後的 LINE Channel Secret 與 Channel Access Token。
- Notebook 的正規化 ID、顯示名稱、綁定狀態及檢查時間。
- 邀請碼與短效工作階段的不可逆雜湊、範圍和到期時間。
- 課程 Gmail 的 email、加密後 NotebookLM 授權與非敏感健康狀態。
- 尚未完成的 LINE 提問、所屬 Channel ID 與 LINE 使用者 ID 會以固定 Fernet 金鑰短暫加密保存，供背景工作在服務重啟或暫時失敗後恢復；回答完成、達重試上限或超過待處理保留期後會清除這些內容（V2 預設上限為 24 小時）。

## 系統不應保存或顯示

- 學員的 Google 密碼或 Google Cookie。
- 學員未分享的 Notebook、Gmail 或 Drive 內容。
- 明文 LINE Secret／Access Token。
- 明文管理員密碼或 Bearer token。
- 完整 Notebook URL、明文問題內容、Google RPC payload 或授權內容的應用程式 log。
- 已完成或已終止工作的問題內容。

## 資料可見性

當學員以 Viewer 分享 Notebook 後，課程 Gmail 可以讀取該 Notebook 的來源與回答，並代表 LINE Bot送出問題。所有透過 LINE 提問的內容也會傳送到 NotebookLM 服務處理。

學員可隨時在設定頁解除綁定，並在 NotebookLM 移除課程 Gmail。若只完成其中一步，另一端的資料或權限可能仍然存在。

## 憑證事件

若懷疑課程 Gmail 授權或 LINE 憑證外洩，管理者應立即撤銷／更新憑證、撤銷相關工作階段、檢查存取紀錄並通知受影響使用者。不得把完整憑證貼到 log、Issue 或客服訊息。

舊版 Channel 在過渡期可能仍有尚未清除的明文 LINE 欄位；這是正式上線阻擋，只能在隔離 V2 遷移驗證期存在。確認加密憑證 Webhook 正常且完成備份後，必須依運維程序清除明文欄位，未完成前不得將 V2 視為核准正式環境。
