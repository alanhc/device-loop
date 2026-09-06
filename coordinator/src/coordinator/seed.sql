-- Phase 1 seed 資料:設計文件第 5 節的範例 row。
-- devices.host 是觀測值;seed 只是初始快照,之後由 exporter heartbeat 更新。

INSERT INTO hosts (id, address, arch, resources, always_on, last_seen_at) VALUES
  ('rog-laptop',    '100.71.211.115', 'x86_64', '{"ram_gb":32}', FALSE, NULL),
  ('alanhc-14700',  '100.69.80.97',   'x86_64', '{"gpu":"rtx-5070ti","vram_gb":16,"ram_gb":64}', TRUE, NULL),
  ('macbook-m1pro', '100.96.167.93',  'arm64',  '{"gpu":"m1-pro","ram_gb":16}', FALSE, NULL);

INSERT INTO devices (id, class, control, identifier, provisioning, host,
                     power_control, tags, state, last_seen_at) VALUES
  ('pixel8-shiba',   'android',     'adb', '38011FDJH00C9F', 'static',
   'rog-laptop',    'adb-reboot', '{"arch":"arm64","sve2":true}', 'free', NULL),
  -- identifier 是 **tailnet 節點名**,不是 IP:§5 明講「識別碼不能用 IP」
  -- (DHCP 換位址就變成一台新裝置)。這一行原本填 '100.69.80.97',那是
  -- 14700 自己的 tailscale IP 而不是 Jupiter 的(Jupiter 是 100.101.114.46)
  -- ——兩個錯疊在一起。Jupiter 是 tailnet-native 裝置(§10 已解決的那條),
  -- host 留著只是表達「誰負責照看它」,不是「插在誰身上」。
  ('milkv-jupiter',  'riscv-sbc',   'ssh', 'milkv-jupiter', 'static',
   'alanhc-14700',  NULL, '{"arch":"riscv64","soc":"spacemit-k1"}', 'free', NULL),
  ('macbook-m1pro',  'macos-arm64', 'ssh', 'alans-macbook-pro', 'static',
   'macbook-m1pro', NULL, '{"arch":"arm64","soc":"apple-m1-pro","neon":true,"sve":false}', 'free', NULL),
  ('14700-5070ti',   'gpu-cuda',    'ssh', 'cuda:0', 'static',
   'alanhc-14700',  NULL, '{"gpu":"rtx-5070ti","vram_gb":16}', 'free', NULL),
  ('rog-zen4',       'x86-cpu',     'local', 'rog-laptop:cpu', 'static', 'rog-laptop', NULL,
   '{"vendor":"amd","uarch":"zen4","avx512":true,"sha_ni":true}', 'free', NULL),
  ('14700-raptor',   'x86-cpu',     'ssh',   'alanhc-14700:cpu', 'static', 'alanhc-14700', NULL,
   '{"vendor":"intel","uarch":"raptor-lake","avx512":false,"avx_vnni":true,"hybrid":true}', 'free', NULL),
  -- Cuttlefish 池(§12):借它就生一台 AVD。Android userspace 的 `any`
  -- 工作導來這裡,真機 Pixel 只留 kernel/thermal/perf。
  -- tags 就是 spawn 的 spec:image 路徑、RAM、CPU 數。
  ('cf-pool',        'cuttlefish-template', 'cvd-spawn', 'cf-pool:template',
   'ephemeral', 'alanhc-14700', NULL,
   '{"host_path":"/home/alanhc/cf","product_path":"/home/alanhc/cf","memory_mb":4096,"cpus":4,"arch":"x86_64","android":"16"}',
   'free', NULL);
