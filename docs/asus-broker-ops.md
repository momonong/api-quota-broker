# ASUS Broker：按需啟動的 Doppler 受限運維入口

## 結論與目前狀態

**C socket activation、30 天唯讀 ops token、僅 inspect／restart 已選定；完整工程候選已完成，尚未安裝或通過 ASUS native acceptance。** 新增 60 項離線測試通過，既有 39 項 ops／31 項帳號內部候選／7 項 TTY 證據沿用。沒有建立 token／帳號、上傳新運維檔案、呼叫真 Doppler／供應商或重啟服務。

人類來源已直接核對 main `01a0de5f-8306-71a3-9738-7ac6eb4d7746` turn `01a10ad1-bc47-7970-9e32-283be63f1e35`／message `01a10ad1-bcd9-76a1-b958-cdd6bc6e18a8` 原話「好了 那現在呢 你能繼續了嗎」。main 明確接續先前推薦：按需自動入口、30 天唯讀 token、只查狀態／重啟 Broker。orchestrate `01a0d4cf-c625-7610-a50e-b9ff278ce901` 交原 task 完整候選；首次安裝只驗無副作用 inspect／權限拒絕，**真 Broker restart 實測另待 main／人類明確批准**。

本 task `01a0d64c-471b-7e70-b879-0a2ccfe8c890` 保留成果供協調者核對。新版本部署、任意 source/path、pool-once／未知 claims／六家 provider、HP／OrderFlow／網路變更、morris 密碼及 PAM／sshd 廣泛改動皆不在此範圍。原四檔 resume 上傳／offline 批准已完成，不能當作此次十一檔傳送批准。

## 已有秘密與來源邊界

真人已建立 `api-quota-broker-ops/dev`，only-names 確認 `ASUS_BROKER_DEPLOY_PASSWORD` 存在；只看名稱，不讀值。真人產生的 ASUS 檔案仍在 `/home/morris/.local/share/api-quota-broker-ops/deploy-password.txt`：私有目錄0700、檔案0600／single link／41 bytes。

`.local`／`share` 祖先0775；gid1000=morris，沒有額外群組成員，primary gid1000 只有 morris。這些路徑仍可由 morris 同 UID 替換，不能聲稱代理無法取得既有密碼。**本整合流程不用該檔案**，改由有 swap/core 保護的 ASUS root bootstrap／短命 worker 直接 fresh Doppler GET。未改共用目錄權限，未讀、搬移或刪除密碼檔；其與 Doppler 值是否相同仍未知，也不是本流程的依賴。日後清除／輪替需另有明確授權。

人類生成／交接來源：main turn `01a10a3b-6b9b-7c10-a3ea-503ee088fd4a`／message `01a10a3b-6bbf-77c0-9cbc-050460938411` 與 turn `01a10a3d-afc6-7cd2-8b11-57ef45e47e59`／message `01a10a3d-afe8-71f3-878a-840b28ccf294`。不要求重建 project 或密碼。

## 固定架構與信任邊界

| 元件 | 固定契約 |
|---|---|
| Socket | `/run/api-quota-broker-ops/control.sock`，root:morris／0660；root:morris／0750 parent，由 root-owned tmpfiles 在開機建立，socket 等待 tmpfiles setup |
| 啟動 | systemd `Accept=yes`，每連線一個短命 `api-quota-broker-ops@.service`；MaxConnections=1、每來源1、60秒最多12次觸發，閒置沒有 worker |
| Agent | 原 morris SSH → 固定 client → Unix socket；worker 獨立核對 SO_PEERCRED UID1000，沒有 TCP/public endpoint |
| 帳號 | `broker-deploy`、nologin、`/nonexistent`、private group、無 home/key，與 runtime UID/GID 分離；真 sudo/PAM 驗證期間必須有有效密碼且不過期 |
| Bootstrap token | ops config 整體唯讀、最多30天；root-owned encrypted systemd credential `/etc/api-quota-broker-ops/ops_doppler.cred`，不使用 runtime token 或個人廣權 token |
| Worker | ops UID、stdin/stdout socket、stderr null；MemoryMax128MiB／Swap0／Core0／CPU25%／45秒／KillMode control-group／不重試；runtime credentials/state 在 mount namespace 不可存取 |
| sudo | PASSWD、timestamp_timeout0、passwd_tries1、ignore cache；只允許 zero-argument `/usr/local/libexec/api-quota-broker-control`，沒有任意 root shell/command |
| Helper | root-owned fixed Python stdlib package；只收 operation enum＋32hex request_id；固定 inspect 或 restart `api-quota-broker.service` |
| Pins | 既有 current link／release manifest與source、readonly runtime、unit SHA、沒有drop-in，及初始化當下 gateway config SHA；漂移就拒絕 |

Python 使用 `-I -B -S`，不新增 pip／第三方依賴。package manifest pin兩個實際載入的 module；檢查完整 root-owned ancestor chain、single-link regular files。NoNewPrivileges 必須為 no，否則 sudo 無法 setuid；worker/helper 實查 kernel flag，不能套用互相衝突的 hardening。

Accept=yes／StandardInput=socket 的原生傳入方式與連線上限依 [systemd v259 socket 規格](https://raw.githubusercontent.com/systemd/systemd/v259/man/systemd.socket.xml)。credential 支援 service-owned0400，或 root:root0440＋精確 named-user read ACL；必須排除 group／other／額外 named-user 的讀權。[systemd v259 credential implementation](https://raw.githubusercontent.com/systemd/systemd/v259/src/core/exec-credential.c)。**ASUS 實際 owner/ACL 尚待首次 native inspect gate，不把 fixture 當實機證據。**

C 是事先委派固定操作的免互動入口，Doppler 是遠端可撤銷依賴，不是每次人工批准或第二因素。root／既有管理者仍是可信管理者。撤銷 token 不撤回已取得的密碼副本；必須關閉入口／鎖帳號並按需要輪替密碼。保留的 morris-readable 檔案使這一點更明確。

## 每次操作與重播

1. 驗 socket peer、唯一請求、enum／request_id／192-byte上限；只允許一個JSON，client須SHUT_WR，拒絕串接請求。
2. 驗 package／帳號／policy expiry，再驗 cgroup、Swap0、Core0、non-dumpable、NoNewPrivileges0。token 過期先拒絕，不讀 credential。
3. exact helper 以 `sudo -k -n`／EOF 無副作用探測；如果可繞過密碼，或不能確認需要密碼就拒絕。
4. 只讀該服務的 systemd credential 一次，對固定 `api.doppler.com` secret endpoint GET一次。std HTTPS無proxy/redirect/retry/cache；固定CA、body8192-byte上限，TLS key log 禁用。API返回必須200、name正確、raw==computed、URLsafe密碼格式。
5. `sudo -k -S` 密碼只進匿名 stdin pipe；helper READY 後才傳請求。若認證被改為NOPASSWD，未被消耗的密碼不是合法JSON，不能執行操作。stdout只返回驗證後固定狀態欄位，stderr不轉傳。
6. **inspect 完全不寫 state**：只核對pins與固定 service show，不建立 claim/result，不讀 runtime DB／credentials／journal。
7. restart 先拿固定root lock、O_EXCL持久保存dispatch intent，再執行固定systemctl；只有新start timestamp及healthy/pins核對通過才保存結果。同 request_id不能重播；失敗保留claim、不自動重試。state項數達界限即停止，不自動刪歷史。
8. bytearray best-effort清零；Python/TLS/PAM可能有副本，因此以短命worker退出與control-group清理作生命期邊界。cleanup不確定就回unknown，不自動重試。

sudo 無法單獨證明密碼剛由 Doppler 取得；fresh fetch是可信worker的契約。[sudo-rs v0.2.13](https://raw.githubusercontent.com/trifectatechfoundation/sudo-rs/v0.2.13/docs/man/sudo.8.md)。Service Token讀整個config，RO／實際期限目前由真人Dashboard聲明，**不是API遠端證明**；root policy的issued_at是此次bootstrap admission時間，不冒充remote mint time。expiry-issued_at<=30天、到期fail closed；Doppler提前撤銷／到期返回拒絕也立即fail closed。[Doppler Service Tokens](https://docs.doppler.com/docs/service-tokens)。

## 一次完整 bootstrap

候選：[install_ops.py](../deploy/asus/install_ops.py)。現在不由代理執行root或新服務。封版入口把public source複製到fresh root0700 private目錄，核對外部固定SHA後才exec；不sudo執行user-owned upload程式。

root inline Python在重定向TTY前已完整編譯；dup2 `/dev/tty`後再exec `systemd-run --pty --wait --collect`，不使用歷史 `bash -s` parser。指定 `api-quota-broker-ops-bootstrap.service`、Memory128MiB／Swap0／Core0／CPU25%／300秒；程式再核對 controlling TTY與kernel gate。[systemd v259 --pty 規格](https://raw.githubusercontent.com/systemd/systemd/v259/man/systemd-run.xml)。本地真PTY測過隱藏讀取，**ASUS transient controlling TTY仍未原生驗證**。

順序：

1. 固定host/root/code seal、existing account/group/artifacts absent；核對原Broker／OrderFlow及pins；有效sshd對新帳號必須password/kbd authentication禁止、無CA／keys command／外部keys路徑。若既有SSH政策不滿足就**在讀token／建帳號前停止**，不改sshd或PAM。
2. 保護的ASUS TTY確認 `READONLY30`、輸入Dashboard實際expiry UTC及**一次隱藏token**。先通過前置檢查再建立token，避免預檢失敗留下無用token；不貼聊天、不放argv/env。初始fresh GET1次，不讀本機密碼檔。
3. useradd先建立expired/nologin帳號；chpasswd匿名stdin設定Doppler密碼，再解除expiry使PAM可真驗。驗new UID/private group、P狀態與Account expires never。
4. 發布root-owned package/helper/client、disabled policy、encrypted token、socket/template、tmpfiles及精確sudoers；完整visudo/systemd-analyze verify，才reload新unit metadata。
5. 原生effective sudo command集合、錯密碼、cached/NOPASSWD/額外argv/任意command拒絕、SSH admission再驗。啟動socket但restart gate仍disabled；morris固定client做一次fresh Doppler／真sudo-rs/PAM **inspect**，等待worker全部退出，原服務不變。
6. 全通過才原子切policy enabled、fsync、enable socket；再核對原服務與pins。bootstrap不呼叫真正restart，不碰provider。

失敗：停止／disable新socket及其worker，只辨識並鎖定／expire本次新UID；partial useradd也重新辨識，身份不明就不改帳號並報manual recovery。只撤回本次新寫且SHA/owner吻合的sudoers/helper/unit/tmpfiles；保留新帳號、ciphertext、安全receipts與新restart歷史，原服務／ledger／pool claims不變。任何清理不明回 `rollback_verified=false`，不重跑或覆寫既有帳號／目錄。

安全收據保存於 `/var/backups/api-quota-broker/ops-bootstrap-<uuid>/`，只有固定stage/error/status、核對後數值metadata；不保存password/token/raw API body／輸入輸出。OS `/etc/shadow`認證hash與加密systemd bootstrap credential是必要系統認證材料，不另複製進repo／聊天／紀錄。

## 封版、交付與真人門檻

本輪十一檔為九個source/policy/unit/helper、seal.json與ops-bootstrap-once.sh；不含秘密、資料、私有chat IDs或provider raw output。builder [build_ops_review.py](../deploy/asus/build_ops_review.py) 只在fresh本地review目錄封版，沒有upload/API/sudo副作用。

**此刻尚未上傳新運維檔案或建立token。** 協調者先核對封版與停止條件；必要傳送沿正式工具，不以舊四檔批准或另一工具繞過拒絕。成功上傳並核對同SHA/0700/0600後，才由main交真人一條已固定SHA的ASUS SSH TTY入口。真人只需在自己的TTY輸入原sudo密碼一次，再照程式的保護提示建立／隱藏貼入一個30天RO ops token；不重建project或密碼。

ASUS native完成須以安全receipt證明：真controllingTTY、credential owner/ACL、有效專用帳號PAM、NOPASSWD/錯密碼/越權拒絕、socket UID授權、worker清理、pins及原服務不變。fixture撤銷／重播測試不冒充真token撤銷或真restart實測。若SSH／PAM／系統工具gate不滿足，交精確fixed code由main判斷，禁止放寬認證、開root shell或改原管理路徑求通過。

## 驗證證據與保留歷史

- 新 [test_asus_ops_entry.py](../tests/test_asus_ops_entry.py)：**60 passed**，包括真Unix socket peer讀取、真匿名pipe READY握手及NOPASSWD競態反例、真controllingPTY三種完成/EOF/中斷且不回顯、HTTP單GET不redirect/proxy/TLS key log、exact credential ACL、30天schema對齊、inspect無檔案寫入、restart intent/failure/replay、每個初始化階段失敗rollback、partial-create安全辨識及unsafe UID拒絕。
- 其中root-owned storage屬注入metadata＋實際隔離檔案/flock/EXCL/fsync；account/PAM/cgroup/HTTP皆fixture。**不是ASUS native成功。** Ruff format/check與root inline compile／入口bash-n通過。
- [broker_ops_policy.py](../deploy/asus/broker_ops_policy.py) 39項historical isolated證據沿用；本integrated worker只重用fixedDoppler parser，不把其舊review plan當目前架構狀態。
- `deploy/asus/initialize_broker_deploy.py`（本工作樹歷史候選，未納入此提交） 31項僅locked/expired內部候選；不用作單獨真人安裝入口。其0775來源gate不適用本新flow。
- TTY 7項歷史root parser修復沿用；舊pool-once已停用，private bootstrap/claim/ledger未核對，仍不得重播。`docs/asus-provider-pool.md`（本工作樹歷史證據，未納入此提交）。
- 四檔resume階段已完成；2026-10-05再次唯讀核對原ASUS四SHA／0700／0600／single-link一致，既有3.14.4 offline receipt passed。`/tmp/api-quota-broker-resume-four-files-rechecked-2026-10-05.json`，沒有新傳送或apply。`docs/asus-deployment.md`（本工作樹歷史紀錄，未納入此提交）。

UTC `2026-10-05T04:14:59.302789+00:00` 的唯讀基線：broker-deploy不存在，Broker PID204419／NRestarts0、OrderFlow PID172058／NRestarts0，current `release-86d576...`及unit `6596ebca...`不變；不能用歷史snapshot取代首次root的fresh preflight。

**工程候選與封版完成；native安裝、native inspect與實際restart驗收未完成。** 不宣稱已提供可用接入或部署成功。

最終本地封版：`/tmp/api-quota-broker-ops-socket-review-2026-10-05-r2`。安全收據：`/tmp/api-quota-broker-ops-socket-review-seal-2026-10-05-r2.json`。入口SHA256：`c7a995344eb613725a9243369435804ee84d1f00d870318e37fec0316494d6c9`。r1 bytes相同但舊收據未列完整60項與native gate清單；r2是本次指定交付目錄，不混用兩份收據。
