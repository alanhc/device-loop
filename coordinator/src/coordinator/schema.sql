-- 多使用者裝置預約系統 — Phase 1 schema
-- 依設計文件 README 第 5 節(hosts/devices/leases/device_services)與
-- 第 8 節(events)。列舉欄位加上 CHECK 約束以在 DB 層擋掉非法值。

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS hosts (
    id           TEXT PRIMARY KEY,     -- 'rog-laptop', 'alanhc-14700', 'macbook-m1pro'
    address      TEXT NOT NULL,        -- tailscale IP
    arch         TEXT NOT NULL,        -- 'x86_64' | 'arm64'
    resources    JSON,                 -- 容量(placement 用),見設計文件第 5 節
    always_on    BOOLEAN NOT NULL DEFAULT TRUE,  -- 筆電節點 = FALSE(機會性)
    last_seen_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS devices (
    id            TEXT PRIMARY KEY,        -- 'pixel8-shiba', 'milkv-jupiter'
    class         TEXT NOT NULL,           -- 'android' | 'openbmc' | 'riscv-sbc' | 'bbb' | 'qemu-template' | ...
    control       TEXT NOT NULL,           -- 'adb' | 'ipmi' | 'ssh' | 'serial' | 'qemu-spawn' | 'local'
    identifier    TEXT NOT NULL UNIQUE,    -- USB serial / IP / spawn script path(穩定識別碼,身分綁這裡;
                                           -- inventory diff 靠它比對,不參與 USB 發現的用 '<host>:cpu' 形式)
    provisioning  TEXT NOT NULL
                  CHECK (provisioning IN ('static', 'ephemeral')),
    host          TEXT REFERENCES hosts(id),  -- 觀測值,由 exporter inventory 回報更新;ephemeral 可為 NULL
    power_control TEXT,                    -- 強制回收用:adb-reboot / ipmi-power-cycle / sd-eject / NULL
    tags          JSON,                    -- {"arch":"arm64","sve2":true} 之類的能力描述
    state         TEXT NOT NULL DEFAULT 'free'
                  -- retired:ephemeral 實例用完了。row 留著是因為
                  -- events.device_id 是硬性 FK 而 §8 的稽核紀錄不能刪;
                  -- 借不到、也不列在 list_devices 裡,效果等同消失。
                  CHECK (state IN ('free', 'leased', 'offline', 'maintenance',
                                   'unregistered', 'retired')),
    last_seen_at  TIMESTAMP
);

CREATE TABLE IF NOT EXISTS leases (
    id          INTEGER PRIMARY KEY,
    device_id   TEXT NOT NULL REFERENCES devices(id),
    user_id     TEXT NOT NULL,
    purpose     TEXT,                      -- 自由文字,方便事後追查
    created_at  TIMESTAMP NOT NULL,
    expires_at  TIMESTAMP NOT NULL,        -- TTL
    renewed_at  TIMESTAMP,                 -- 最後一次 heartbeat
    released_at TIMESTAMP,                 -- NULL = 進行中
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'released', 'expired', 'force_reclaimed'))
);

CREATE TABLE IF NOT EXISTS device_services (
    device_id TEXT NOT NULL REFERENCES devices(id),
    service   TEXT NOT NULL,     -- 'adb' | 'uart' | 'scrcpy' | 'flash' | 'mcp'
    endpoint  TEXT NOT NULL,     -- host:port(exporter 按需動態產生後回報)
    mediated  BOOLEAN NOT NULL DEFAULT FALSE,  -- true = 走 API 而非直連(flash 一定是 true)
    PRIMARY KEY (device_id, service)
);

-- 第 8 節:append-only 稽核紀錄。每個 lease 狀態轉換、exporter 服務啟停、
-- flash 成敗、inventory diff 出的裝置搬移/插拔都寫一筆。
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY,
    device_id  TEXT NOT NULL REFERENCES devices(id),
    lease_id   INTEGER REFERENCES leases(id),  -- 跟 lease 無關的事件可為 NULL
    kind       TEXT NOT NULL
               CHECK (kind IN ('reserve', 'renew', 'release', 'expire', 'force_reclaim',
                               'service_start', 'service_stop', 'flash_start', 'flash_result',
                               'device_moved', 'device_attached', 'device_detached')),
    actor      TEXT,             -- user_id,或 'reaper' / 'exporter' 代表系統自己觸發
    detail     JSON,             -- 自由格式,如 flash 的 image 參照、force_reclaim 的原因
    created_at TIMESTAMP NOT NULL
);

-- 強制回收(§7 第 4 步)。lease 過期後服務會被 reconcile 自動停掉,但
-- 「裝置本身還是卡的」不會自己好——下一個 agent 會借到一台壞的裝置。
-- 這張表把「該對這台裝置執行什麼強制動作」變成 exporter 收斂得到的狀態,
-- 沿用 heartbeat 通道,不另外開 coordinator→exporter 的推送。
CREATE TABLE IF NOT EXISTS reclaim_actions (
    id          INTEGER PRIMARY KEY,
    device_id   TEXT NOT NULL REFERENCES devices(id),
    lease_id    INTEGER REFERENCES leases(id),  -- 觸發它的 lease(人工觸發可為 NULL)
    action      TEXT NOT NULL,        -- 照抄 devices.power_control:adb-reboot / ipmi-power-cycle
    state       TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'running', 'done', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    detail      JSON,                 -- exit code、stderr,失敗時查得到原因
    created_at  TIMESTAMP NOT NULL,
    finished_at TIMESTAMP
);

-- 一台裝置同時只能有一個未完成的回收動作:重複下重開指令沒有意義,
-- 而且會讓「重開到一半又被重開」變成可能。跟 lease 同樣用 partial UNIQUE
-- 把不變量鎖在 DB 層。
CREATE UNIQUE INDEX IF NOT EXISTS idx_reclaim_active
    ON reclaim_actions (device_id) WHERE state IN ('pending', 'running');

-- 佇列(設計文件 §12)。lease 仍是唯一的互斥原語,佇列只決定下一個
-- lease 給誰。Phase 2 只做 interactive(排隊等 lease);batch 之後再說。
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY,
    device_id   TEXT REFERENCES devices(id),   -- class-based 排程成形後可為 NULL
    user_id     TEXT NOT NULL,                 -- agent 的身分也走這欄
    kind        TEXT NOT NULL
                CHECK (kind IN ('batch', 'interactive')),
    payload     JSON,                          -- batch 用;interactive 為 NULL
    timeout_s   INTEGER NOT NULL,              -- 排太久就放棄,死掉的 agent 不佔位
    state       TEXT NOT NULL DEFAULT 'queued'
                CHECK (state IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    lease_id    INTEGER REFERENCES leases(id), -- 輪到時由 scheduler 建立
    result      JSON,
    created_at  TIMESTAMP NOT NULL,
    started_at  TIMESTAMP,
    finished_at TIMESTAMP
);

-- 一個 user 對同一台裝置只能排一個 interactive job。少了這條,agent 重試
-- 會塞爆佇列、FIFO 公平性直接失效——thundering herd 只是從 409 重試搬進
-- 佇列裡。重複請求回同一張號碼牌(API 把 IntegrityError 轉成查既有的)。
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_queued_interactive
    ON jobs (device_id, user_id) WHERE state = 'queued' AND kind = 'interactive';
CREATE INDEX IF NOT EXISTS idx_jobs_queue
    ON jobs (device_id, state, created_at);

-- 「一台裝置同時最多一條 active lease」是系統唯一真正要緊的不變量,
-- 在 DB 層鎖死;reserve 把 UNIQUE 衝突轉成 409。
CREATE UNIQUE INDEX IF NOT EXISTS idx_leases_active
    ON leases (device_id) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_events_device
    ON events (device_id, created_at);

-- Image registry(設計文件 §6「Image registry 與 known-good restore」)。
-- Flash 不接受任意 image,只接受這裡登記過的:唯一的破壞性操作要能事後
-- 回溯「刷了什麼進去」,而路徑是呼叫者自由填的字串的話,那個問題無解。
-- 同時這是把目前人肉維護的還原清單(哪個分割區刷壞了要用哪個 image 救)
-- 固化成資料——每台裝置的 known_good 集合就是 restore 的目標。
CREATE TABLE IF NOT EXISTS images (
    id         TEXT PRIMARY KEY,      -- 'shiba-vendor-hello-v3'
    device_id  TEXT NOT NULL REFERENCES devices(id),
    kind       TEXT NOT NULL,         -- 'boot' | 'vendor' | 'dtbo' | 'sd-card' | ...
    uri        TEXT NOT NULL,         -- exporter 本地路徑或 artifact store 位置
    sha256     TEXT NOT NULL,
    known_good BOOLEAN NOT NULL DEFAULT FALSE,
    note       TEXT,
    created_at TIMESTAMP
);

-- 一台裝置的同一種分割區同時只有一個 known-good:restore 要選哪個不能有
-- 歧義。登記新的 known-good 時由 store 把舊的降級,不是靠呼叫者自己記得。
CREATE UNIQUE INDEX IF NOT EXISTS idx_images_known_good
    ON images (device_id, kind) WHERE known_good = TRUE;

-- Mediated flash(§6 能力表唯一 mediated = true 的能力)。
-- 跟 reclaim_actions 同一個形狀,理由也一樣:coordinator 不碰硬體(§4),
-- 它只把「該刷什麼」寫成狀態,由裝置所在 host 的 exporter 隨 heartbeat
-- 領走執行——沿用同一條通道,不另開 coordinator→exporter 的推送。
CREATE TABLE IF NOT EXISTS flash_jobs (
    id          INTEGER PRIMARY KEY,
    device_id   TEXT NOT NULL REFERENCES devices(id),
    image_id    TEXT NOT NULL REFERENCES images(id),
    lease_id    INTEGER REFERENCES leases(id),  -- 授權來源;restore 由系統觸發時為 NULL
    user_id     TEXT NOT NULL,        -- 誰要求的(§6:每次執行要記錄 requester)
    state       TEXT NOT NULL DEFAULT 'pending'
                CHECK (state IN ('pending', 'running', 'done', 'failed')),
    attempts    INTEGER NOT NULL DEFAULT 0,
    detail      JSON,                 -- exit code、stderr、實際跑的 argv
    created_at  TIMESTAMP NOT NULL,
    finished_at TIMESTAMP
);

-- 一台裝置同時只有一個未完成的 flash。並行刷同一台裝置是直接把它變磚的
-- 做法,跟 reclaim 用同樣的 partial UNIQUE 把不變量鎖在 DB 層。
CREATE UNIQUE INDEX IF NOT EXISTS idx_flash_active
    ON flash_jobs (device_id) WHERE state IN ('pending', 'running');

-- Ephemeral VM 實例(§10 開放問題的裁決:沿用同一張 devices 表,
-- `provisioning='ephemeral'`,不另外做 device_templates)。
--
-- 一筆 template(`class='qemu-template'`)代表「可以生出這種 VM」;
-- lease 的時候由 exporter 真的 spawn 一台,生出來的實例是 devices 表裡
-- 另一筆 `class='qemu-vm'`、`provisioning='ephemeral'` 的 row。
--
-- 為什麼實例也進 devices 而不是只存在記憶體裡:lease、events、
-- device_services 全部已經以 device_id 為軸,實例不進表的話這三套機制
-- 都要各自長出「如果是 VM 的話……」的分支。實例是短命的,release 時
-- 連 row 一起刪掉——§12 說 ephemeral 的清理方式就是「直接銷毀重生,
-- 天然乾淨」。
CREATE TABLE IF NOT EXISTS vm_instances (
    id          TEXT PRIMARY KEY,      -- 同時是 devices.id
    template_id TEXT NOT NULL REFERENCES devices(id),
    host        TEXT NOT NULL REFERENCES hosts(id),
    lease_id    INTEGER REFERENCES leases(id),
    state       TEXT NOT NULL DEFAULT 'requested'
                CHECK (state IN ('requested', 'running', 'stopping', 'gone')),
    spec        JSON,                  -- exporter spawn 要的參數(image/RAM/arch)
    detail      JSON,                  -- spawn 結果:pid、實際 port、錯誤
    created_at  TIMESTAMP NOT NULL,
    finished_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_vm_instances_host
    ON vm_instances (host, state);
