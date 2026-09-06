"""device-loop exporter:跑在每台實際插著裝置的 host 上的常駐 agent。

三個職責(設計文件 §4):

1. **Inventory**——掃出本機看得到的穩定識別碼(adb serial、
   ``/dev/serial/by-id/``),隨 heartbeat 回報給 coordinator。
2. **Per-resource daemon**——按 lease 起停該裝置的存取服務
   (uart 走 ser2net、adb 走專用 adb server),回報 ``host:port``。
3. **清理**——退出時把所有起過的子行程收乾淨。
"""
