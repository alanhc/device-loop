# exporter

裝置預約系統的 exporter:跑在每台實際插著裝置的 host 上的常駐 agent。
設計文件見上層 [README](../README.md) 第 4(角色)、6(能力表)、8
(heartbeat/inventory)節。

Phase 1–3 完成:執行層、uart/adb/video/vnc 能力層、inventory 掃描、
coordinator client 與 agent loop、強制回收、mediated flash、ephemeral
QEMU 池。

```bash
uv run exporter --coordinator http://100.69.80.97:8000 --host-id rog-laptop
uv run exporter --host-id rog-laptop --once -v      # 只跑一輪,除錯用
```

## 開發

```bash
uv sync
uv run pytest
```

## 模組

- `proc.py` — 可注入的 `ProcessRunner` / `Handle`。正式用
  `SubprocessRunner`,測試用 fake,所以沒有實體裝置的機器也測得動。
- `services.py` — per-resource daemon:uart 走 ser2net(RFC2217)、
  adb 走 `--one-device` 專屬 server。`ServiceManager` 管起停與退出清理。
- `inventory.py` — 從 sysfs 與 `/dev/serial/by-id/` 掃穩定識別碼,並回報
  這次掃描涵蓋哪些 device class。**掃描必須零副作用**,見下。
- `coordinator_client.py` — heartbeat 與 device 查詢。
- `agent.py` — 主迴圈:掃描 → 回報 → 收斂到 coordinator 給的 desired。
- `reclaim.py` — 強制回收:對卡住的裝置執行 `power_control` 動作。
- `flash.py` — mediated flash:刷之前先驗 sha256,不對就絕不動手。
- `vm.py` — ephemeral QEMU 池:按 coordinator 的 desired 生出/銷毀 VM。
- `cvd.py` — Cuttlefish 池:ephemeral 的第二種 provisioner(§12)。
- `cli.py` — 進入點。

**Inventory 掃描不得有副作用。** 這裡原本跑 `adb devices` 列裝置,那是錯
的:那個指令會啟動全域 adb server,而全域 server 會認領 USB 裝置。時序上
的後果是——收斂讓 per-device server 拿到裝置之後,下一輪 inventory 又跑
`adb devices` 起了全域 server,全域看到空清單(裝置在 per-device server
手上),inventory 就判定裝置不見了,回報 detached,**lease 進行中的裝置被
誤判拔線**。

改成直接讀 `/sys/bus/usb/devices`:判準是 ADB 的 interface descriptor
(`ff/42/01`)而不是 VID 白名單(廠商清單維護不完)。sysfs 反映「插了
什麼」,跟「誰在用它」無關,所以掃描跟自己起的服務不再打架。有一條回歸
測試會在任何人再度引入子行程時失敗。

## 強制回收(§7 第 4 步)

lease 過期時「停掉服務」由 reconcile 自動達成(desired 縮小,exporter
自己停)。但**裝置本身卡住不會自己好**——下一個 agent 會借到一台不能用
的。這是 `reclaim.py` 補的缺口。

判準是「過了寬限期服務還在」:endpoint 還在 `device_services` 裡代表
exporter 沒收斂成功(掛了、或裝置卡到 daemon 停不掉),這才動用
`power_control`。正常過期不會走到這裡。

動作走白名單(`adb-reboot` / `ipmi-power-cycle`),**不認得的字串一律拒絕
執行**——回收動作猜錯的代價是對錯的裝置做破壞性操作。有逾時,因為卡住的
裝置常讓指令一起卡住。

回收在**收斂之後**才做:先確定服務停了,才對裝置下重開指令。

失敗的裝置留在 `maintenance` 不放回 `free`——寧可少一台可用裝置,也不要
把壞的交給下一個 agent。

> **真機注意**:`adb-reboot` 真的會重開手機。ROG 上那顆 Pixel 8 有
> alanhc 手工構築的分割區狀態,**真機測試前要先取得同意**,不要因為
> 「重開而已」就自己跑。

## Reconcile:coordinator 不推送

每次 heartbeat 的回應帶「這台 host 現在該跑什麼」(desired),exporter
拿它跟自己實際跑著的比對,自行補起缺的、停掉多的,並把實際起好的
host:port 回報上去。Coordinator 不主動叫 exporter 起停服務。

為什麼是 reconcile 而不是 push:

- Heartbeat 本來就是 control-plane channel(§4),不用另外開連線。
- Exporter 不需要開 listening port——機會性節點(筆電)在 NAT 後面或
  睡醒之後照樣能用。
- Coordinator 重啟不需要記得通知過誰,下一輪 heartbeat 自動收斂。
- 失敗語意簡單:沒收斂成功的下一輪還在 desired 裡,自然重試,不需要
  額外的重試佇列。

代價是 lease 到服務可用有一個 heartbeat 週期的延遲,所以 **endpoint 是
最終一致的**——MCP 那邊取 endpoint 會遇到 pending 狀態,要跟「exporter
沒在跑」分開講。

兩個實作上要緊的細節:

- **回報前先清掉死掉的 daemon**,否則會回報一個已經連不上的 endpoint,
  coordinator 就把它當有效的交給 client。
- **coordinator 連不上時不停服務**——那會踢掉正在用的 client。`run()`
  吞掉例外繼續跑,不讓 coordinator 重啟一次就把 exporter 弄死。
- `mediated` 不由 exporter 回報,由 coordinator 依 service 種類決定
  (flash 恆為 true)。讓回報方宣告自己是否需要 mediation,等於把安全
  相關的事實交給被管制的一方。

## Mediated flash(§6)

Flash 是能力表裡唯一 mediated 的能力,執行的形狀跟 `reclaim.py` 一樣
(coordinator 寫狀態、exporter 隨 heartbeat 領走),但多兩條約束,因為
reclaim 最壞是重開一次,flash 刷壞就是磚:

- **刷之前自己算一次 sha256,對不上就絕不動手。** Coordinator 的 registry
  說「這個 uri 的雜湊應該是 X」,但檔案在 exporter 本地——可能被換掉、
  下載到一半、或根本是另一個 build。不驗的話 registry 只是記帳。驗證跑在
  **第一個子行程之前**,所以失敗時裝置完全沒被碰過。
- **只認識白名單裡的 device class → flash 方法**(android 走 fastboot、
  riscv-sbc 走 dd)。不認得的一律拒絕,不猜:猜錯的代價是對一台不該用
  fastboot 的板子跑 fastboot。

刻意**不做**「進入 fastboot mode」這一步:怎麼進每台都不一樣(Pixel 走
`adb reboot bootloader`,Jupiter 要短接 boot pin,見 §10),塞進來等於在猜,
而猜錯是把裝置留在一個半吊子的狀態。

## Ephemeral QEMU 池(§10/§12)

`vm.py` 跟服務收斂同一個模型,只是對象是整台 VM:coordinator 說「這台 host
上該有這些實例」,exporter 補起缺的、停掉多的,並回報實際狀態。

**銷毀就是全部的清理**——§12 把 ephemeral 列為「直接銷毀重生,天然乾淨」,
所以這裡沒有任何 reset 邏輯。

兩個實作上的陷阱:

- **`-vnc` 吃的是 display number 不是 port**(`:0` = 5900)。寫成 port 號
  會開到 5900+port 那個天邊的位置去,所以 port 一律換算,而且低於 5900 的
  port 直接拒絕——那種 port 根本沒辦法叫 QEMU 去綁。
- **`accel` 由 spec 指定,不猜。** §12 的硬體分層說 aarch64 要導去 M1 的
  HVF 池才有意義,x86 host 上只能 TCG 模擬、慢一個量級。猜錯的後果是
  benchmark 數字沒有意義,而那正是這整個系統要避免的事。

`-snapshot` 是必須的:共用同一份 base image 的兩台 VM 互相污染是最難查的
那種錯誤。

## Cuttlefish 池(§12)

§12 的硬體分層政策:**真機時間是稀缺資源,只花在非真機不可的事上**。
Android userspace 的 `any` 工作導去 AVD 平行跑,真機 Pixel 只留
kernel/thermal/perf。14700 上 `cvd` 與 10 組 instance 網橋已經備好。

跟 `vm.py` 是**同一個收斂模型的兩種 provisioner**:coordinator 說「這台
host 上該有這些實例」,誰把它生出來是 exporter 的事,所以兩者介面一致
(`converge` / `stop_all` / `running`),由實例的 `class` 分派。

四個 Cuttlefish 特有、而且**都在真機上驗證過**的地方:

- **實例編號是稀缺資源。** 編號決定網橋與 adb port(`6520 + n - 1`),
  所以要明確指定並在池內唯一,不能讓 cvd 自己挑——兩台同號會搶同一組
  網橋。池子上限是預配的網橋數(10),滿了明確報錯。
- **`cvd` 是 client/server 架構。** `cvd create` 很快就返回,實例歸常駐的
  cvd server 管。所以**不能**用「子行程還活著嗎」判斷實例在不在——那樣
  每輪都會以為它死了然後重生一台。要問 `cvd fleet`。
- **`cvd stop` 之後 group 還留在 `cvd fleet` 裡**,只是 instance 的
  `status` 變成 `Stopped`。只看 group 在不在的話,停掉的實例會永遠被當成
  活著。**這個是真機才抓得到的**——單元測試的假輸出當初只給了 group 名字,
  照著寫就會漏掉。
- **對外就是一台 Android 裝置。** 實例的 adb 掛在 `127.0.0.1:<6520+n>`,
  exporter 起一個 `--one-device` server 把它轉出來,client 看到的介面跟
  真 Pixel 一模一樣。§6 的 vnc 那列明講「Cuttlefish 自帶 WebRTC 串流,
  不走這條」,所以**不起 vnc**。

adb port 要等實例真的生出來才知道,所以它跟 endpoint 一樣是**最終一致**
的:實例回報 `adb_identifier`,coordinator 才據此把 adb 服務排進 desired。
沒有這層的話 exporter 會拿實例 id 當 adb serial 去連,每輪失敗一次。

### ⚠️ 已知問題:從 systemd service 裡 spawn 會失敗

**手動在互動 shell 跑 `cvd create` 成功,同一條指令從 exporter service
裡跑會失敗。** crosvm 起不了 VM:

```
libminijail[1]: unshare(CLONE_NEWNS) failed: Operation not permitted
crosvm: exiting with error 1: the architecture failed to build the vm
```

crosvm 會把每個虛擬裝置關進自己的 mount namespace 做沙箱,而這台上
`kernel.apparmor_restrict_unprivileged_userns = 1`(Ubuntu 預設)擋掉了
unprivileged user namespace。

排除掉的假設,記下來免得重查:

- **不是 PATH。** 一開始確實少了 `/usr/sbin`(Cuttlefish 要
  `iptables`/`ebtables`/`dnsmasq`/`modprobe`),已經修進 unit 檔;修完
  症狀不變。
- **不是 AppArmor profile 差異。** 互動 shell 與 service 都是 `unconfined`。
- **不是資源限制。** service 的 `MemoryMax`/`TasksMax` 都沒有設限。
- **不是這個 unit 特有的設定。** 用 `systemd-run --user` 跑同一條指令
  一樣失敗——問題出在 systemd user session 這個執行脈絡本身。

一條值得追的線索:`cvd` 是 client/server,**cvd server 跑在第一個啟動它
的 session 裡**(實測時它在 `session-8047.scope`,也就是互動 shell)。
所以「誰先起 cvd server」會決定 crosvm 繼承到什麼脈絡,手動與服務的差別
很可能出在這裡。

可能的解法(都還沒試):把 cvd server 也做成一個 systemd user service 讓
它有固定的脈絡、或給 exporter service 加 `Delegate=yes`、或退一步用
`sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`(需要 root,
而且放寬的是全系統的限制)。

**收斂邏輯本身是對的**:失敗時 exporter 逾時、回報 failed、裝置被扣住,
不會把一台起不來的實例交給下一個人。

## 能力實作要點

**adb 啟動前一定要先 `adb kill-server`,每次都要跑,而且要等它跑完。** 一顆 USB
裝置同時只能被一個 adb server 認領,host 上的全域 server(port 5037,
任何人跑一次 `adb devices` 就會起來)會先搶走裝置,結果是專屬 server
起得來、endpoint 也回報得出去,但 client 連進來 `adb devices` 是空的
——靜默失敗。Labgrid `ADBExport._start()` 同樣的理由做同樣的事。

**不能用 `terminate()` 代替 `wait()`**:kill-server 要花幾十毫秒連上 5037
並要求它退出,立刻 SIGTERM 會在它做完之前把它砍掉,全域 server 活得好好
的。症狀跟完全沒跑 preflight 一樣——服務起得來、endpoint 也發布了,client
連進去卻看不到裝置。這個 bug 上過真機(ROG + Pixel 8)才被抓到,單元測試
的 fake runner 抓不到,因為假的 terminate 不會打斷真實工作。

不要優化成「一個生命週期只跑一次」:全域 server 起來之後**才**接上的
第二顆裝置會先被它認領,preflight 沒重跑的話新的 --one-device server
就拿到空清單,同一個失敗換成多裝置情境觸發。重跑是安全的——
`adb kill-server` 不帶 `-P` 只打 5037,動不到跑在其他 port 上的專屬
server(本機實測:5038 上的 server 在 kill-server 後仍在 listen)。

**uart 用 ser2net 不用 socat。** RFC2217 才能讓 client 遠端改 baud、拉
DTR/RTS,SBC 進 boot mode 或救磚靠的就是這些訊號腳(設計文件 §6)。
`-Y` 的多段 YAML **只有第一段寫 `connection:`**,後續是縮排續行;每段
都寫會被解析成另起一個新連線。

## 驗證狀態

哪些驗過、哪些沒有,分開講清楚。

**adb 路徑已在真硬體上完整驗證**(ROG + 真 Pixel 8,2026-09-06);
**uart 路徑仍未在真實序列埠上驗證**——目前沒有任何一台機器有實體序列埠。

| 項目 | 狀態 |
|---|---|
| 執行層、ServiceManager 生命週期、inventory 解析 | 單元測試涵蓋(74 tests) |
| ser2net 的 YAML **解析形式** | **已驗**:本機用 ser2net 4.6.0 實跑。舊的重複 `connection:` 寫法得到 `No connector given in connection`,現在的續行寫法解析得過 |
| **uart 服務在真實序列埠上** | **尚未驗證**。開發機沒有實體序列埠,連得上、能改 baud/DTR、`nouucplock` 是否真的避開 lock 檔,都要在有序列埠的機器上才驗得到 |
| adb `kill-server` 的必要性 | 已在 ROG + 真 Pixel 8 上實測確認 |
| preflight 必須 `wait()` 不能 `terminate()` | **已驗**:本機實測 `terminate` 之後 5037 還在、`wait` 之後消失;正式程式碼路徑跑過真 `adb`,5037 確實被殺掉 |
| inventory 掃描不啟動全域 adb server | **已驗**:本機跑 `AdbScanner().scan()` 前後 5037 都是 down |
| 完整迴圈走真 socket + 真 adb server | **已驗**:真 coordinator(uvicorn:8077)+ 真 per-device `adb server`,reserve → 起 daemon(port 32965)→ endpoint `100.71.211.115:32965` 進 DB → release → row 消失;全程 5037 保持 down |
| `kill-server` 不影響其他 port 上的專屬 server | **已驗**:本機起 5038 server,跑 `adb kill-server` 後 5037 消失、5038 仍在 listen 且行程存活 |
| **多裝置下的 preflight 行為** | **尚未驗證**。邏輯上每次啟動都重跑就涵蓋了,但「全域 server 先認領第二顆裝置」要兩顆真裝置才測得到 |
| **adb 服務接真 Pixel 端到端** | **已驗**(ROG + 真 Pixel 8,2026-09-06):`adb -H … -P … devices` 列出 `38011FDJH00C9F device`、`adb shell getprop ro.product.model` 回 `AOSP on shiba`(指令真的在裝置上執行);endpoint `100.71.211.115:33997`;全程全域 5037 未被起來;lease 期間 inventory 持續看到裝置,**零誤判 detached/moved**;release 後裝置回 free、port 消失 |
| **sysfs 的 `ff/42/01` 判準對真裝置成立** | **已驗**(ROG + 真 Pixel 8):`/sys/bus/usb/devices/1-1` interface `1-1:1.0` 是 `class=ff sub=42 proto=01`,`AdbScanner().scan()` 回 `['38011FDJH00C9F']` 且未起任何子行程 |
| heartbeat 迴圈對真 coordinator | **已驗**:`tests/test_integration.py` 對真的 coordinator app(真 SQLite、真 HTTP 語意)跑完整一圈——reserve → 收斂起 daemon → endpoint 進 `device_services` → release → 服務停掉、endpoint 消失;另涵蓋 lease 過期回收與新板子自動出現等 adopt |
| 收斂邏輯(起/停/重試/清理) | 單元測試涵蓋(fake runner) |
| 強制回收的完整迴圈 | **已驗**(整合測試,fake 執行層):升級 → 派給 exporter → 執行 → 回報 → 裝置放回 free;失敗的留在 maintenance 且借不到 |
| **`adb reboot` 對真裝置** | **尚未驗證**,且**需要先取得同意才能測**——它會真的重開 ROG 那顆 Pixel |
| **`ipmi-power-cycle`** | **尚未驗證**。14700 有 BMC,但目前沒有任何裝置用這條路徑 |
| flash 的拒絕路徑(雜湊不符、檔案不在、不認得的 class) | 單元測試涵蓋,並斷言**一個子行程都沒起**——「絕不動手」要驗的是這件事 |
| flash 的完整迴圈 | **已驗**(整合測試,fake 執行層):登記 image → 要求 → exporter 領走 → 驗雜湊 → 執行 → 回報 → coordinator 記結果;被竄改的 image 那條斷言 `fastboot` 從未被呼叫 |
| **`fastboot` 對真裝置** | **尚未驗證**,且**需要先取得同意才能測**——ROG 那顆 Pixel 8 有手工構築的分割區狀態 |
| **`dd` 寫 SD/eMMC** | **尚未驗證**。Jupiter 的 flash 機制本身還是 §10 的開放問題 |
| ephemeral VM 的收斂(生出/銷毀/孤兒/重生) | 單元測試涵蓋(fake runner);完整迴圈另有整合測試對真 coordinator 跑過 |
| **真的開一台 QEMU VM** | **尚未驗證**。收斂邏輯有測試,「qemu 起得來嗎」沒有 |
| **Cuttlefish:真的開一台 AVD** | **已驗**(2026-09-06,14700 + `~/cf` 的 `aosp_cf_x86_64_only_phone` build 14253210):用**這份程式碼產生的 argv** 跑 `cvd create`,exit 0、實例 Running、`adb_port` 回報 6520(與 `adb_address(1)` 一致);`adb connect 127.0.0.1:6520` 後 `getprop ro.product.model` 回 `Cuttlefish x86_64 phone 64-bit only`、Android 16。`cvd stop` exit 0 |
| Cuttlefish 的 `cvd fleet` 解析 | **已驗**:對真輸出解析,跑著時認得出 group,`cvd stop` 之後認不出——**後者是真機才抓到的 bug**(停掉的 group 仍留在 fleet 裡,只是 status 變 Stopped) |
| Cuttlefish 的完整迴圈 | **已驗**(整合測試,fake 執行層):借 template → create → 回報 adb 位址 → 起 `--one-device` server → endpoint 進 DB → release → 銷毀 |
| **多台 Cuttlefish 同時跑** | **尚未驗證**。編號分配邏輯有測試,但「10 台一起跑吃不吃得消」沒試過(一台 4GB,10 台就 40GB) |
| **從 systemd service 裡 spawn** | **失敗中**,見上方「已知問題」:crosvm 的 `unshare(CLONE_NEWNS)` 在 systemd user session 裡被擋。手動跑得起來,服務跑不起來 |
| `cvd stop` 不釋放 instance number | **已驗**(真機):停掉的 group 仍佔編號,同號 create 吐 `New instance conflicts with existing instance`(exit 255)。修法是 stop 之後一定要 `cvd remove` |
| **video(ustreamer)接真 camera** | **尚未驗證**。目前沒有對著任何裝置的 UVC camera |
| **vnc(socat)接真上游** | **尚未驗證**。argv 形式有測試,實際轉發沒有 |

## Phase 1 之後

- 真裝置驗證:**adb 已完成**;ser2net 接實體序列埠仍待有序列埠的機器
  (見上表)。多裝置 preflight 等 pixel-10 接上。
- Phase 2:flash mediation、reaper 的強制回收(依 `power_control` 斷電)、
  裝置搬機時在新 host 重建 endpoint 並通知 lease 持有者。

## 部署(systemd user service)

`alanhc-14700` 上的第二個 exporter(§9 的 Phase 2 部署工作)就是這樣跑的。

```bash
./deploy.sh                              # 部署 HEAD 並重啟服務
./deploy.sh --dry-run                    # 只看會部署哪個 rev
systemctl --user status device-loop-exporter
journalctl --user -u device-loop-exporter -f
```

首次安裝:

```bash
cp deploy/device-loop-exporter.service ~/.config/systemd/user/
systemctl --user daemon-reload
./deploy.sh
systemctl --user enable --now device-loop-exporter
```

三個決定值得記著:

- **不用 Docker。** §9 明講 exporter 不適合容器化排程——它一定要釘在插著
  裝置的那台 host 上,而且要直接碰 `/dev`、USB 與本機的
  `nvidia-smi`/`tailscale`。coordinator 才是適合容器化的那個。
- **user service 而不是 system service。** 這台的 linger 開著(logout 後
  仍會跑),而 exporter 需要的東西全都在使用者權限內。不用 root 就不要用。
- **從 `~/.local/share/device-loop` 跑,不是從工作區。** 工作區會被
  rebase/checkout 改動,一次 git 操作就換掉正在服務的程式碼。`deploy.sh`
  用 `git archive` 取快照——跟 coordinator 用容器映像當快照同一個理由。
  (順帶:快照要含 `coordinator/`,因為 exporter 的 dev 相依用
  `path = "../coordinator"`,少了它 `uv` 連 `--no-dev` 都解不動。)

### 14700 上這台管什麼

三台裝置,**沒有一台在 USB 上**——這正是它跟 ROG 那台的差別:

| 裝置 | class | 怎麼被看到 |
|---|---|---|
| `14700-raptor`(i7-14700) | `x86-cpu` | `LocalResourceScanner`:本機 CPU |
| `14700-5070ti` | `gpu-cuda` | `LocalResourceScanner`:`nvidia-smi` 探測 |
| `milkv-jupiter` | `riscv-sbc` | `TailnetScanner`:走 `seen`,**不宣稱擁有** |

Phase 1 的兩個 scanner(USB/serial)在這台什麼都掃不到,所以這三台的
`last_seen_at` 原本永遠是 `NULL`,§8 的 reaper **偵測不到它們離線**。
兩個新 scanner 補的就是這個缺口,而它們的差別正是 §5 那條規則:

- 本機 CPU/GPU 跟 USB 一樣,「插在我身上的只有我用得到」成立,所以可以
  參與缺席判定。
- **Jupiter 不行**:它是 tailnet-native 裝置,ROG、macmini、14700 都摸得
  到它。回報成 `identifiers` 的話,coordinator 會建 row 並把 host 填成
  回報者,三台 exporter 互相搶著宣稱擁有權,每輪 heartbeat 產生一筆
  `device_moved`。所以走 `seen`:只把 `last_seen_at` 往前推,不建 row、
  不改 `host`、不參與缺席判定。

### 還缺的工具

14700 上目前**沒有** `socat` / `ser2net` / `ustreamer`,所以 vnc、uart、
video 三種能力起不來(會每輪回報一次 `not found in PATH`,收斂邏輯本身是
對的)。要用到時:

```bash
sudo apt install socat ser2net ustreamer
```

`adb` / `fastboot` / `qemu-system-x86_64` / `nvidia-smi` / `tailscale` 都在。
