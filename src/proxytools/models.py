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

    @property
    def key(self) -> tuple[str, str]:
        return self.protocol, self.proxy

    def to_dict(self) -> dict:
        return asdict(self)
