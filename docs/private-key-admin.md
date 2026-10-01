# 私用 API Key 管理頁

狀態：2026-09-30 本機候選實作；尚未由使用者輸入真實 key 驗收，也沒有部署、推論呼叫、合併或推送。工作分支 `feat/private-key-admin`，起點 `378011624d5a49e556b2b3addb6febdb2573e7d6`。

## 用途與界線

`quota-broker key-admin-serve` 是與推論 gateway 分開的管理進程，只綁 `127.0.0.1`。管理者登入後，只能選兩個固定目的地：NVIDIA `api-quota-broker/dev/NVIDIA_API_KEY` 或 Groq `api-quota-broker/dev/GROQ_API_KEY`。表單可輸入 key 和選填的名稱、ID、到期日期；按「明確儲存／替換」才寫入 Doppler。兩個 provider 共用既有 `api-quota-broker/dev`；此 scope 不存在時一律拒絕寫入，不會新建 project/config。頁面不提供刪除、任意 secret 名稱、讀回完整 key 或模型推論。

2026-10-01 已將 Doppler project 原地從 `api-provider-nvidia` 改名為 `api-quota-broker`。改名前後 `dev` 的 6 個秘密名稱指紋一致，包含 NVIDIA、Groq、Gemini 的 API key 名稱；未讀取秘密值，也未驗證各 provider 金鑰有效性。

狀態 `configured` 只代表 Doppler 的名稱清單中有該 secret；`missing` 代表 project/config 或名稱不存在。查詢失敗時顯示未知。寫入只有在 CLI 回報成功、且之後名稱清單確認存在時才顯示成功；失敗／不確定時不回顯 CLI 輸出或 key，也不宣稱新值已保存。這些狀態**不證明** key 對 provider 有效、免費或有額度。Groq Console 連結由使用者提供，未加入 Groq 模型 adapter 或免費資格假設。

## 本機啟動

管理進程使用管理者 OS 帳號的 Doppler CLI 登入，不能用推論執行層的唯讀 Service Token。管理者先在此機以 CLI 登入，核對其 Doppler 權限。管理憑證另存本機 Git 忽略的 `0600` 檔案，至少 32 字元，不放在 argv、URL、Git 或聊天。例：

```bash
uv run --locked quota-broker key-admin-serve \
  --admin-token-file .state/key-admin-login-token \
  --metadata-file .state/key-admin-metadata.json \
  --port 18085
```

瀏覽器開 `http://127.0.0.1:18085/admin/login`，由管理者在本機讀取自己的 token 檔後貼進登入欄。預覽使用者可直接保存 key，但保存只表示已交給 Doppler；不會順便送出測試推論。啟動前要核對 loopback port 未被其他服務使用。若 Doppler 登入／寫入權限不足，頁面顯示失敗或未知，先由管理者修正登入或權限，不以唯讀 token 代寫。

## 安全與驗證

HTTP 強制精確 loopback Host；POST 接受精確同源 Origin。Codex IAB 的同頁表單實測會送 `Origin: null`，因此僅在瀏覽器同時回報 `Sec-Fetch-Site: same-origin`、`Sec-Fetch-Mode: navigate`、`Sec-Fetch-Dest: document` 時接受此特例；單獨的 null 或跨站標頭仍拒絕。登入後使用 30 分鐘記憶體 session、HttpOnly/SameSite=Strict cookie 與 CSRF token。所有回應 `Cache-Control: no-store`，停用 request log，CSP 限制內容及表單。頁面永不把 key 放入 HTML value、SQLite、選填 metadata、localStorage、argv、環境變數或一般日誌。key 只在當次表單 POST 與後端記憶體，透過 Doppler CLI stdin 傳值；CLI stdout/stderr 僅捕獲、不顯示。選填 metadata 另以 `0600` JSON 保存，不含 key；成功或失敗後皆用 redirect 清空表單。

驗證使用 fake Doppler writer，不寫真實 key：共用固定 scope 與替換、scope 缺失時拒絕、CLI stdin、寫入失敗、登入、Host、Origin、CSRF、metadata 權限及回應無 key。正式 gateway 的 `unknown` 請求沒有重送或變更。

2026-09-30 IAB 驗證：原本精確 Origin 檢查會拒絕 IAB 的 `Origin: null` 登入 POST。修正後在獨立假 token／假 Doppler fixture 的 IAB 中，登入、管理頁及假儲存均顯示成功；真實管理頁另經獨立 HTTP session 確認登入 303、管理頁 200，未讀或寫 provider key。
