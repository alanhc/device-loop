# coordinator

裝置預約系統的 coordinator。設計文件見上層 [README](../README.md)。
目前實作到 **Phase 3**:schema、lease API、reaper、exporter heartbeat 端點、
MCP 前門、佇列、強制回收、image registry 與 mediated flash、scrcpy/video/vnc
能力、ephemeral QEMU 池。Exporter 本體在 [`../exporter`](../exporter)。

## 開發

```bash
uv sync
uv run pytest
```

## 跑起來

```bash
uv run uvicorn coordinator.api:app --host 0.0.0.0 --port 8000
```

首次啟動會建表並灌入設計文件第 5 節的 seed 資料(DB 已有裝置時跳過)。

環境變數:`COORDINATOR_DB`(預設 `coordinator.db`)、
`COORDINATOR_REAPER_INTERVAL_S`(10)、`COORDINATOR_OFFLINE_AFTER_S`(120)、
`COORDINATOR_HEARTBEAT_GRACE_S`(30)、
`COORDINATOR_RECLAIM_AFTER_S`(60,強制回收的升級門檻)、
`COORDINATOR_MCP_ALLOWED_HOSTS`(`127.0.0.1,localhost`,見上方警告)、
`COORDINATOR_MCP_PATH`(`/mcp`)。

## ⚠️ 安全警告:目前沒有認證

**任何連得到 coordinator 的人都能宣稱自己是任何 user。** `user_id` 是呼叫
者自己填的字串,系統完全不驗證(設計文件 §10 明列認證模型未設計)。

MCP 開成網路服務之後這件事被放大了:stdio 時代呼叫者本來就在機器上,
現在 tailnet 上任何節點都連得進來。實際後果包括:

- 冒充別人 `release_device` 釋放掉他的 lease,再把裝置搶走
- 冒充別人 `cancel_queued` 把他踢出佇列
- 用別人的身分操作裝置,稽核紀錄記到錯的人頭上

**目前唯一的實質防線是 tailnet ACL**——只有 tailnet 內的節點連得到
coordinator。應用層沒有任何東西在擋。部署時不要把 coordinator 暴露到
tailnet 以外。

身分取得收斂在 `authz.caller_identity()` **一個函式**,之後接
tailnet identity(用對端 IP 查 `tailscale whois`)只要改那裡,所有 tool
一行都不用動。有守衛測試確保沒有 tool 繞過它。

## 端點

| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/devices` | 列出裝置與目前狀態 |
| GET | `/devices/{id}/events` | 該裝置的稽核紀錄 |
| POST | `/leases` | reserve;裝置不存在 404、已被借走 409 |
| GET | `/leases/{id}` | 查 lease |
| POST | `/leases/{id}/renew` | 延長 TTL;要帶 `user_id`,不是持有者回 404 |
| POST | `/leases/{id}/release` | 釋放;要帶 `user_id`,不是持有者回 404 |
| POST | `/heartbeat` | exporter 回報 inventory + 目前跑著的服務;回應帶 `desired` |
| POST | `/leases` + `queue:true` | 裝置忙碌時回號碼牌而不是 409 |
| GET | `/jobs/{id}` | 查排隊進度 |
| POST | `/jobs/{id}/cancel` | 放棄排隊;要帶 `user_id` |
| GET | `/images` | 列出 registry 裡的 image(可帶 `device_id`) |
| POST | `/images` | 登記一份可以刷的 image |
| POST | `/flash` | 要求刷 image;要帶 lease,回 202 |
| GET | `/flash/{id}` | 查 flash 進度 |
| GET | `/instances` | 目前存在的 ephemeral VM 實例 |

## Reconcile:exporter 自行收斂

Heartbeat 同時是 control-plane channel(§4)。Exporter 回報 inventory 與
「我目前實際跑著什麼」,coordinator 回 `desired`(這台 host 上有 active
lease 的裝置該跑哪些服務),exporter 自行補起缺的、停掉多的。Coordinator
**不主動推送**——exporter 不用開 port,coordinator 重啟後靠下一輪 heartbeat
自動收斂。

`device_services.endpoint` 由 exporter 回報的實際 port 加上 `hosts.address`
(tailscale IP)組成,client 可以直接貼著用。lease 結束或 exporter 不再
回報時 row 就刪掉——endpoint 是 lease 期間才有效的動態值,留著會讓 client
連到已經停掉的 port。

`mediated` 是例外:它是能力種類的**靜態**性質(flash 恆為 true),由
coordinator 依 `store.MEDIATED_SERVICES` 自己填,**不接受 exporter 回報**
(多送這個欄位會 422)。讓回報方宣告自己是否需要 mediation,等於把安全
相關的事實交給被管制的一方。

因為要等 exporter 下一輪 heartbeat 才會回報 port,**endpoint 是最終一致
的**:reserve 完立刻取 endpoint 會撲空,那是 pending 不是錯誤。

Heartbeat 回傳這次 diff 出的 `moved` / `attached` / `detached` /
`unregistered`,加上 `desired` / `recorded` / `dropped`。回報可帶 `discoverable_classes` 指明涵蓋範圍——USB 掃描
列不出 SSH 上的 SBC 或本機 CPU,不指明的話預設只把 USB 可發現的 class
納入缺席判定。

## MCP 前門

設計文件 §6 把 MCP 定為 agent 操作裝置的統一入口。

**掛在同一個 app、同一個 process**:`app.mount("/mcp", ...)`,沿用 HTTP
API 那條 DB 連線與那把鎖——不引入第二套並行控制,也不用第二條 SQLite
連線。容器只跑一個 uvicorn 就同時提供 REST、儀表板與 MCP,`CMD` 不用改。

Agent 連 `http://<coordinator>:<port>/mcp/`(streamable HTTP)。stdio 仍然
可用(`uv run python -m coordinator.mcp_main`),但那只有同機的 client
用得到。

### ⚠️ 部署必設:`COORDINATOR_MCP_ALLOWED_HOSTS`

MCP 的 transport 有 DNS rebinding protection,**預設只放行本機**。遠端
agent 連進來時 Host header 不是 localhost,會拿到 **421 Misdirected
Request**——是「連得上但一直被拒」而不是連線失敗,不知道有這個機制的話
會查很久。

```bash
COORDINATOR_MCP_ALLOWED_HOSTS=100.69.80.97:8090,localhost,127.0.0.1
```

值是 **agent 實際撥的 host:port**,不是容器內部綁的位址。啟動時會 log
一行 INFO 列出目前允許哪些;如果只放行本機,再 log 一行 WARNING 說明
遠端會拿到 421——設定錯的話在啟動時就看得見。

註:`allowed_hosts=["*"]` **沒有用**(wildcard 不是那樣匹配的),要嘛
明確列出,要嘛整個關掉保護。這裡選明確列出。

| Tool | 需要 lease | 說明 |
|---|---|---|
| `list_devices` | 否 | 列出裝置與狀態 |
| `reserve_device` | 否 | 預約,回傳 lease_id |
| `renew_lease` | **是**(持有權) | 延長自己 lease 的 TTL |
| `release_device` | **是**(持有權) | 釋放自己的 lease |
| `get_queue_status` | **是**(持有權) | 查排隊進度;輪到時帶回 lease_id |
| `cancel_queued` | **是**(持有權) | 放棄排隊,把位置讓給後面的人 |
| `adb_shell` | **是** | 回傳 adb endpoint 供 client 直連 |
| `get_uart_stream` | **是** | 回傳 RFC2217 endpoint |
| `scrcpy_launch` | **是** | 回傳在自己桌面跑 scrcpy 的指令(不起服務) |
| `get_video_stream` | **是** | 回傳 MJPEG endpoint(camera 照實體螢幕) |
| `get_vnc_stream` | **是** | 回傳 VNC endpoint(軟體 framebuffer) |
| `list_images` | 否 | 列出 registry 裡的 image |
| `flash_image` | **是** | 排一個 mediated flash,回 flash_id |
| `get_flash_status` | 擁有權 | 查自己送出的 flash 進度 |

**每個需要授權的 tool 都先過 `authz.py`**,分兩種:

- `require_lease(device_id, user_id)`——碰裝置的 tool。問「你是不是現在
  持有這台裝置的人」。
- `require_lease_owner(lease_id, user_id)`——操作既有 lease 的 tool。問
  「這條 lease 是不是你的」。

**後者不能省**:`leases.id` 是 INTEGER PRIMARY KEY,連號整數,猜測成本
趨近於零。少了這層,任何人都能 `release_device(lease_id=5)` 釋放掉別人
的 lease,裝置回到 free 之後立刻搶走——直接打穿「一台裝置同時最多一條
active lease」這個系統唯一真正要緊的不變量,而且被害者不會收到任何錯誤,
會繼續對一台已經不屬於它的裝置下指令。

檢查集中在 `authz.py` 而不是散在各 tool 裡,因為它是安全邊界——漏一個
tool 就等於那個 tool 沒有保護。測試有一條守衛要求**每個** tool 都被明確
分類成「碰裝置」「操作 lease」或「刻意不需授權」,沒分類就失敗;它的前一
個版本只檢查「收 device_id + user_id」的 tool,正是因此漏掉了只收
`lease_id` 的 renew/release。

檢查內容:裝置上有 active lease、lease 屬於呼叫者、給了 lease_id 的話
要對得上、且**尚未過期**(用 `expires_at` 而不是只看 `status`——reaper
是週期性跑的,過期到被標記之間有一個 tick 的窗口)。

「不存在」與「不是你的」回同一句話,連號的 id 才不能拿來探測哪些 lease
存在(HTTP 側同理,兩者都回 404 而不是 403)。

拒絕原因用 `ToolError` 回報,訊息會原樣送到 agent 手上;用其他例外的話
SDK 當成 crash,agent 只看得到「Error executing tool X」無從修正。拒絕
訊息不揭露實際持有者是誰。

**這不是認證。** §10 明列認證模型未設計,`user_id` 依約假設可信;這層擋的
是「借了 A 去操作 B」「lease 過期還在用」「用別人的 lease」,不是冒名。
之後接 Tailscale identity 時只要換掉 user_id 的來源,檢查點位置不用動。

`adb_shell` / `get_uart_stream` / `get_video_stream` / `get_vnc_stream`
回傳 endpoint 讓 client 直連,不由 coordinator 代跑指令——§6 說這些能力
就是直連,只有 flash 是 mediated。endpoint 來自 `device_services`,由 exporter 在 heartbeat
回報實際 port 後寫入(見上方 reconcile)。因為要等下一輪 heartbeat,
endpoint 是最終一致的——這兩個 tool 在還沒回報時會回一個明確的 pending
錯誤,並跟「exporter 沒在跑」「這個 class 根本沒這種服務」分開講。

## Image registry 與 mediated flash(§6)

Flash 是能力表裡唯一 mediated、也是唯一破壞性的操作,兩個約束跟著它而來。

**只刷登記過的 image。** 路徑若是呼叫者自由填的字串,「到底刷了什麼進去」
事後無解——那正是 §6 要 registry 的理由。每台裝置每種分割區只有一個
`known_good`(登記新的會把舊的降級),因為那是 restore 的目標,有兩個的話
restore 不知道要選哪個。

**Exporter 刷之前自己再驗一次 sha256。** Coordinator 說「刷這個 uri,雜湊
應該是 X」,但檔案在 exporter 本地,可能被換掉、下載到一半、或根本是另一個
build。少了這一步,registry 只是記帳。驗證發生在**第一個子行程之前**,所以
對不上的時候裝置完全沒被動過。

派工沿用 reclaim 的形狀(coordinator 寫狀態、exporter 隨 heartbeat 領走),
理由也一樣:coordinator 不碰硬體(§4)。有 reclaim 在飛的裝置不接受 flash
——重開到一半開始刷、或刷到一半被重開,都是製造磚的可靠方法。

**刷壞的裝置不會回到 free**,但這個檢查不能放在收結果的地方:失敗當下裝置
通常還在 lease 裡(持有者正要救它)。關卡在**交還裝置的那一刻**——release
與 reaper 過期兩條路徑都經過——之後成功刷一份 known-good 回去就解除,
restore 因此仍然可用。

## Ephemeral 池:兩種 provisioner(§10/§12)

Ephemeral 有兩種 provisioner,**共用同一套機制**:

| template class | 實例 class | exporter 起什麼 | 實例提供 |
|---|---|---|---|
| `qemu-template` | `qemu-vm` | `qemu-system-*` | vnc |
| `cuttlefish-template` | `cuttlefish-vm` | `cvd create` | **adb** |

多一種 provisioner 只是 `INSTANCE_CLASSES` 多一列,不是多一套機制:
template → 實例的整條路(lease 時 spawn、release 時銷毀、heartbeat 收斂、
retired 保留稽核)跟 provisioner 是什麼完全無關。

**Cuttlefish 實例對外就是一台 Android 裝置**,提供 adb 而不是 vnc——§6 的
vnc 那列明講「Cuttlefish 自帶 WebRTC 串流,不走這條」。它的 adb port 是
`6520 + n - 1`,而 n 要等 exporter 真的生出實例才知道,所以識別碼由實例
**回報**(`detail.adb_identifier`),coordinator 收到才把 adb 排進 desired。
沒有這層的話 exporter 會拿實例 id 當 adb serial 去連,每輪失敗一次。

**ephemeral 實例的 lease 掛在 template 上**,不在實例自己身上——借的人借的
是「給我一台這種 VM」,實例是那條 lease 的產物。所以 `desired_services`
要涵蓋兩種連法:裝置自己有 active lease,或它是某條 active lease 生出來的
實例。少了後者,Cuttlefish 的 adb 服務永遠不會被 desired 出來。

## Ephemeral VM 池的共用機制(§10/§12)

§10 的開放問題「ephemeral 是否該拆成獨立的 `device_templates`」選了
**沿用同一張 devices 表**:template 是一筆 row(`class='qemu-template'`),
借它就 spawn 出一台實例(`class='qemu-vm'`)。lease、events、
`device_services` 全部以 `device_id` 為軸,實例不進表的話這三套機制都要
長出「如果是 VM 的話……」的分支。

實例的生命週期綁在 lease 上:借 template 時生出來,release 或過期就標成
該銷毀,exporter 下一輪停掉 qemu。§12 說 ephemeral 的清理方式是「直接
銷毀重生,天然乾淨」,所以沒有任何 interstitial reset 邏輯。

**用完的實例標 `retired` 而不是刪除。** 原本是刪掉的——ephemeral 不該在
池子裡留下痕跡——但 `events.device_id` 是硬性 FK,刪 device 就得先刪它的
events,而 §8 明講 events 是 append-only 的稽核紀錄。稽核贏:一台 VM 借給
過誰、刷過什麼,在它消失之後仍然要查得到。`retired` 借不到也不列在
`/devices` 裡,對使用者來說效果等同消失。

**回報是觀測,不是指令。** Exporter 送上來的是上一輪的狀態,而 coordinator
可能在那之後已經把實例標成 `stopping`。所以 `running` 回報只套用在還是
`requested`/`running` 的實例上——少了這個條件,慢一拍的回報會把 `stopping`
蓋回 `running`,VM 再也停不掉。這個 bug 是整合測試抓到的,單元測試看不見。

## 佇列(§12)

Lease 仍是唯一的互斥原語,佇列只決定「下一個 lease 給誰」。Phase 2 只做
**interactive**(排隊等 lease);batch 之後再說。

**`reserve_device` 不阻塞。** MCP tool call 卡幾分鐘會拖死呼叫端的 agent,
所以裝置忙碌時立刻回一張號碼牌(`job_id` + `position`),agent 之後用
`get_queue_status` 輪詢。裝置本來就 free 的話直接給 lease,不繞佇列。

**兩個前門同一套語意,只是預設值不同**:`POST /leases` 帶 `queue:true`
才排隊(不帶 = 既有的 409,腳本與 exporter 完全不受影響),MCP 的
`reserve_device` 預設 `queue=true`(agent 的正常期待就是排隊)。儀表板
可以自己選要不要提供排隊按鈕。

**一個 user 對一台裝置只排一個**,重複請求回同一張號碼牌——沒有這條的話
thundering herd 只是從 409 重試搬進佇列裡,agent 連按十次就佔十個位置。

**`cancel_queued` 必須用**:放棄排隊卻不取消,位置會一直佔著。排太久的
job 會被 reaper 依 `timeout_s` 自動取消,死掉的 agent 不會永遠卡位。

**排序是純 FIFO。** §12 說「人的 interactive 排在 agent batch 前面」,但
那需要分得出人跟 agent——目前 `user_id` 是不可驗證的自我宣告,照著做會
變成「宣稱自己是人就插隊」:製造說謊的誘因,同時給人一種系統很公平的
錯覺。排序邏輯獨立在 `_next_queued()`,等身分可信再加那條規則。

`_before_lease_handoff()` 是 §12 interstitial hooks 的位置(job 之間的
裝置清理、benchmark 前的 thermal cooldown),現在是 no-op,Phase 4 才做。

## 強制回收(§7 第 4 步)

Lease 過期時「叫 exporter 停服務」由 reconcile 自動達成——desired 縮小,
exporter 下一輪自己停。所以強制回收真正補的是**裝置本身卡住**的情況:
服務停了、lease 沒了,裝置還是壞的,下一個 agent 會借到一台不能用的。

判準:lease 過期**超過 `COORDINATOR_RECLAIM_AFTER_S`(預設 60s)之後
`device_services` 還在**,代表 exporter 沒收斂成功。寬限期從 `expire`
event 的時間算起,不是從 `expires_at`——lease 可能是很久以前就該過期
(coordinator 停機、或手動改過),用 `expires_at` 的話那些會立刻升級,
根本沒給 exporter 收斂的機會。

Coordinator 不碰硬體(§4),所以它只把「該做什麼」寫進 `reclaim_actions`,
實際的 `adb reboot` / IPMI power cycle 由裝置所在 host 的 exporter 隨
heartbeat 領走執行——沿用同一條通道,不另開推送。

裝置狀態的差別要緊:**過期**只是「沒人租了」(回 `free`),**強制回收**
是「這台可能是壞的」(先扣在 `maintenance`)。回收成功才放回 `free`;
失敗就留在 `maintenance`,寧可少一台可用裝置,也不要把壞的交給下一個
agent。沒有 `power_control` 的裝置同樣標 `maintenance` 等人工(§7)。

一台裝置同時只有一個未完成的回收動作,由 partial UNIQUE index 鎖死——
重複下重開指令沒有意義,而且會讓「重開到一半又被重開」變成可能。

## 檔案

- `src/coordinator/schema.sql` / `seed.sql` — 對應設計文件第 5、8 節
- `src/coordinator/store.py` — lease 轉換、reaper、inventory diff、image
  registry、flash 派工、ephemeral VM 池(不含 HTTP)
- `src/coordinator/api.py` — FastAPI 端點與 reaper 背景任務
- `src/coordinator/authz.py` — lease 授權檢查(MCP 的安全邊界)
- `src/coordinator/mcp_server.py` / `mcp_main.py` — MCP 前門與 stdio 進入點

## 已知限制

- **flash 從未在真硬體上跑過。** `fastboot` 那條路徑只有 fake runner 驗過;
  ROG 上那顆 Pixel 8 有手工構築的分割區狀態,真機驗證前要先取得同意。
  Jupiter 的 flash 機制本身也還是 §10 的開放問題(SD 卡重燒還是 USB burn
  mode 未定),`dd` 那條只是先把形狀擺好。
- **video / vnc / ephemeral VM 都沒有接過真東西**:沒有 camera、沒有上游
  VNC server,也沒有真的開過一台 VM。收斂邏輯有測試,「qemu 起得來嗎」
  沒有。
- 沒有 `power_control` 的裝置(seed 裡的 Jupiter)無法強制回收:只會被標
  成 `maintenance` 等人工介入,不會自動放回 `free`。
- 搬機的 grace window 存在記憶體,coordinator 重啟後最壞多等一輪 heartbeat
  才判定 detach。
- 裝置搬機時進行中的 lease 只記 `device_moved` 事件,還沒有通知持有者或
  在新 host 重建 endpoint 的機制(需要 exporter)。
- Endpoint 是最終一致的,不是即時的:reserve 到服務可用之間有一個
  heartbeat 週期(預設 5s)的延遲。
