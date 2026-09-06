"""Exporter 的進入點。

    uv run exporter --coordinator http://100.69.80.97:8000 --host-id rog-laptop

Host id 要跟 coordinator 的 ``hosts`` 表對得上——它是 exporter 宣告
「我是哪台」的方式,inventory diff 靠它判斷裝置搬機。
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .agent import DEFAULT_INTERVAL_S, ExporterAgent
from .coordinator_client import CoordinatorClient
from .cvd import CvdManager
from .flash import FlashExecutor
from .inventory import (
    AdbScanner,
    LocalResourceScanner,
    SerialByIdScanner,
    TailnetScanner,
)
from .proc import SubprocessRunner
from .reclaim import ReclaimExecutor
from .services import ServiceManager
from .vm import VMManager


def build_agent(
    coordinator_url: str,
    host_id: str,
    interval_s: float = DEFAULT_INTERVAL_S,
    local_resources: bool = True,
    tailnet: bool = True,
) -> ExporterAgent:
    runner = SubprocessRunner()
    scanners = [AdbScanner(), SerialByIdScanner()]
    if local_resources:
        # 這台 host 自己的 CPU/GPU。沒有這個 scanner 的話它們的
        # last_seen_at 永遠是 NULL,reaper 偵測不到它們離線(§8)。
        scanners.append(LocalResourceScanner(host_id))
    if tailnet:
        # tailnet-native 裝置(Jupiter)的 liveness。回報走 seen——只證明
        # 它活著,**不宣稱擁有**(§5:可達性不能決定擁有權)。
        scanners.append(TailnetScanner())
    return ExporterAgent(
        client=CoordinatorClient(coordinator_url, host_id),
        services=ServiceManager(runner),
        scanners=scanners,
        interval_s=interval_s,
        reclaimer=ReclaimExecutor(runner),
        flasher=FlashExecutor(runner),
        vms=VMManager(runner),
        # Cuttlefish 池(§12):Android userspace 的 `any` 工作導來這裡,
        # 真機 Pixel 只留 kernel/thermal/perf。cvd 不在的 host 上它每輪
        # 會回報 failed,不會影響其他裝置。
        cvds=CvdManager(runner),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="exporter", description=__doc__)
    parser.add_argument(
        "--coordinator",
        default=os.environ.get("EXPORTER_COORDINATOR", "http://127.0.0.1:8000"),
        help="coordinator base URL",
    )
    parser.add_argument(
        "--host-id",
        default=os.environ.get("EXPORTER_HOST_ID"),
        help="這台 host 在 coordinator hosts 表裡的 id",
    )
    parser.add_argument(
        "--interval", type=float,
        default=float(os.environ.get("EXPORTER_INTERVAL_S", DEFAULT_INTERVAL_S)),
        help="heartbeat 間隔(秒)",
    )
    parser.add_argument(
        "--no-local-resources", action="store_true",
        help="不回報本機 CPU/GPU(這台 host 的資源不進裝置池時用)",
    )
    parser.add_argument(
        "--no-tailnet", action="store_true",
        help="不回報 tailnet 節點的 liveness(tailnet-native 裝置會偵測不到離線)",
    )
    parser.add_argument("--once", action="store_true", help="只跑一輪就退出(除錯用)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    if not args.host_id:
        parser.error("--host-id is required (or set EXPORTER_HOST_ID)")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    agent = build_agent(args.coordinator, args.host_id, args.interval,
                        local_resources=not args.no_local_resources,
                        tailnet=not args.no_tailnet)
    if args.once:
        try:
            result = agent.tick()
        finally:
            agent.shutdown()
        print(f"started={result.started} stopped={result.stopped} failed={result.failed}")
        return 1 if result.failed else 0

    agent.install_signal_handlers()
    agent.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
