# 學員免安裝綁定說明

新版流程不需要安裝 Python、命令列工具或 Chrome Cookie 擴充套件，也不需要上傳 Google Cookie。

## 操作步驟

1. 向講師取得邀請碼，進入學員設定網址。
2. 輸入邀請碼。
3. 填入自己 LINE 官方帳號的 Channel ID、Channel Secret 與 Channel Access Token。
4. 將畫面顯示的 Webhook URL 貼到 LINE Developers Console，啟用 Webhook。
5. 在 NotebookLM 開啟「分享」，把畫面指定的課程 Gmail 加入，權限選擇 **Viewer／檢視者**。
6. 複製自己要使用的 Notebook 網址，回到設定頁貼上。
7. 按「測試並綁定」。系統會讀取 Notebook 名稱並送出一次最小測試問題；成功後才保存綁定。
8. 到自己的 LINE 官方帳號傳送問題，確認收到回答。

你可以回家後在邀請碼有效期限內重新進入設定頁；重新登入會讓先前開啟的設定工作階段失效。如果畫面顯示邀請碼過期，請向講師索取新碼。

貼上的網址必須是自己的 Notebook 頁面網址，例如：

```text
https://notebooklm.google.com/notebook/<Notebook-ID>
```

不要貼公開分享頁、Google Drive 檔案網址或其他網站網址。

## 分享後課程帳號能看到什麼

課程 Gmail 會以 Viewer 身分讀取你分享的 Notebook、來源內容與 NotebookLM 回答，才能代替 LINE Bot 查詢。它不會因此取得你的 Google 密碼、其他 Notebook、Gmail 或 Google Drive 全部內容。

請只分享課程所需資料，不要把個資、密碼、金鑰、醫療或其他敏感資料放入 Notebook。

## 常見狀態

- 尚未綁定：完成分享後貼網址並測試。
- 檢查中：正在執行 Notebook 存取與最小問答測試，請勿重複送出。
- 已綁定：可以從 LINE 提問。
- 存取已撤銷：重新把 Notebook 分享給畫面上的課程 Gmail，再按「重新檢查」。
- 課程帳號暫時不可用：聯繫講師；不需要重新提供 LINE 憑證或 Google Cookie。
- 無法連線：保留畫面上的 request ID 給講師查詢。

## 停止使用

1. 在設定頁按「解除綁定」。
2. 回 NotebookLM 的分享設定，移除課程 Gmail。

兩個步驟要分別完成：解除系統映射不會自動更改 NotebookLM 的分享名單。
