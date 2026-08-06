"""Domain records shared by commands, checkers, storage, and output layers."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(slots=True)
class ProxyResult:
    protocol: str
    proxy: str
    ok: bool
    latency_ms: int | None = None
    country: str = "Unknown"
    lat: float | None = None
    lon: float | None = None
    checked_at: str = ""
    failure_reason: str = ""
    city: str = "Unknown"
    exit_ip: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return self.protocol, self.proxy

    def to_dict(self) -> dict:
        record = asdict(self)
        record.pop("failure_reason", None)
        return record
