"""跟 coordinator 的 control-plane 通訊(設計文件 §4 的實線)。

Heartbeat 與「哪些裝置該起服務」共用同一條通道,不另外開連線。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx


@dataclass(frozen=True)
class DesiredService:
    """Coordinator 認為這台 host 現在該跑的一個 per-resource daemon。"""

    device_id: str
    service: str
    identifier: str
    lease_id: int | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.device_id, self.service)


@dataclass
class HeartbeatResult:
    """Coordinator 對一次回報的回應:inventory diff + 該收斂到的狀態。"""

    moved: list[str]
    attached: list[str]
    detached: list[str]
    unregistered: list[str]
    desired: list[DesiredService]
    # 待執行的強制回收動作(§7)。原樣傳給 ReclaimExecutor。
    reclaims: list[dict]
    # 待執行的 flash(§6 唯一 mediated 的能力)。原樣傳給 FlashExecutor。
    flashes: list[dict] = field(default_factory=list)
    # 這台 host 上該跑著的 ephemeral VM 實例(§10/§12)。
    instances: list[dict] = field(default_factory=list)

    @classmethod
    def from_json(cls, data: dict) -> "HeartbeatResult":
        return cls(
            moved=data.get("moved", []),
            attached=data.get("attached", []),
            detached=data.get("detached", []),
            unregistered=data.get("unregistered", []),
            desired=[
                DesiredService(
                    device_id=d["device_id"],
                    service=d["service"],
                    identifier=d["identifier"],
                    lease_id=d.get("lease_id"),
                )
                for d in data.get("desired", [])
            ],
            reclaims=list(data.get("reclaims", [])),
            flashes=list(data.get("flashes", [])),
            instances=list(data.get("instances", [])),
        )


class CoordinatorClient:
    """薄薄一層 HTTP client。網路錯誤原樣往上拋,由 agent loop 決定重試。"""

    def __init__(self, base_url: str, host_id: str, timeout_s: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.host_id = host_id
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout_s)

    def heartbeat(
        self,
        identifiers: list[str],
        discoverable_classes: list[str] | None = None,
        seen: list[str] | None = None,
        services: list[dict] | None = None,
        reclaims: list[dict] | None = None,
        flashes: list[dict] | None = None,
        instances: list[dict] | None = None,
    ) -> HeartbeatResult:
        """回報 inventory、跑著的服務與動作結果,拿回 desired 與待辦動作。"""
        payload: dict = {
            "host_id": self.host_id,
            "identifiers": identifiers,
            # 只證明活著、不宣稱擁有的識別碼(tailnet-native 裝置)。
            "seen": seen or [],
            "services": services or [],
            "reclaims": reclaims or [],
            "flashes": flashes or [],
            "instances": instances or [],
        }
        if discoverable_classes is not None:
            payload["discoverable_classes"] = discoverable_classes
        resp = self._client.post("/heartbeat", json=payload)
        resp.raise_for_status()
        return HeartbeatResult.from_json(resp.json())

    def list_devices(self) -> list[dict]:
        resp = self._client.get("/devices")
        resp.raise_for_status()
        return resp.json()

    def close(self) -> None:
        self._client.close()
