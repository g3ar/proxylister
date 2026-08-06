"""Rolling measurements and state transitions for one proxy candidate."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import statistics

from proxytools.models import ProxyResult
from proxytools.stability.policy import StabilityPolicy


@dataclass(slots=True)
class CheckSample:
    checked_at: float
    ok: bool
    latency_ms: int | None
    failure_reason: str = ""


@dataclass(slots=True)
class ProxyHistory:
    protocol: str
    proxy: str
    history_size: int
    samples: deque[CheckSample] = field(init=False)
    latest: ProxyResult | None = None
    consecutive_successes: int = 0
    consecutive_failures: int = 0
    alive_since: float | None = None
    stable_since: float | None = None
    last_advertised_at: float = 0
    state: str = "PROBATION"
    first_seen_at: float | None = None
    total_observed_uptime: float = 0
    last_failure_at: float | None = None
    restored: bool = False
    failure_since: float | None = None

    def __post_init__(self):
        self.samples = deque(maxlen=self.history_size)

    @property
    def key(self) -> tuple[str, str]:
        return self.protocol, self.proxy

    @property
    def successful_latencies(self) -> list[int]:
        return [sample.latency_ms for sample in self.samples if sample.ok and sample.latency_ms is not None]

    @property
    def success_rate(self) -> float:
        return sum(sample.ok for sample in self.samples) / len(self.samples) if self.samples else 0

    @property
    def median_latency(self) -> int | None:
        values = self.successful_latencies
        return round(statistics.median(values)) if values else None

    @property
    def p95_latency(self) -> int | None:
        values = sorted(self.successful_latencies)
        if not values:
            return None
        return values[max(0, math.ceil(len(values) * 0.95) - 1)]

    @property
    def jitter(self) -> int:
        values = self.successful_latencies
        return round(statistics.pstdev(values)) if len(values) > 1 else 0

    def alive_for(self, now: float) -> float:
        return max(0, now - self.alive_since) if self.alive_since is not None else 0

    def record(self, result: ProxyResult, now: float, policy: StabilityPolicy) -> None:
        self.restored = False
        config = policy.config
        succeeded = result.ok and result.latency_ms is not None and result.latency_ms < config.max_latency
        failure_reason = ""
        if not succeeded:
            failure_reason = result.failure_reason or ("failed" if not result.ok else "latency")
        self.samples.append(
            CheckSample(now, succeeded, result.latency_ms if succeeded else None, failure_reason)
        )
        if succeeded:
            self.latest = result
            self.consecutive_successes += 1
            self.consecutive_failures = 0
            if self.alive_since is None:
                self.alive_since = now
            self.failure_since = None
        else:
            # A target-URL failure still proved that the proxy itself answered
            # and produced geolocation data. Keep it visible in the dashboard,
            # but classify the complete health check as degraded.
            if result.failure_reason == "url":
                self.latest = result
            self.consecutive_successes = 0
            self.consecutive_failures += 1
            self.stable_since = None
            if self.consecutive_failures > config.failure_tolerance:
                self.alive_since = None

        if policy.qualifies(self, now):
            if self.state != "STABLE":
                self.stable_since = now
            self.state = "STABLE"
        elif not succeeded and self.state == "STABLE":
            self.failure_since = now
            self.state = "PROBATION"
        elif not succeeded and self.failure_since is not None:
            self.state = (
                "DEGRADED"
                if now - self.failure_since >= config.degraded_after
                else "PROBATION"
            )
        elif not succeeded and self.latest is not None:
            self.state = "DEGRADED"
        else:
            # A successful check means the proxy recovered. It is probationary
            # until the rolling rate/streak/time criteria qualify it as stable;
            # DEGRADED must describe the latest health result, not stick forever
            # after any historical failure.
            self.state = "PROBATION"


def update_advertised(histories, entries, now, history_size):
    for protocol, proxy in entries:
        key = (protocol, proxy)
        if key not in histories:
            histories[key] = ProxyHistory(protocol, proxy, history_size)
        histories[key].last_advertised_at = now


def expire_histories(histories, now, retention_time):
    expired = [key for key, item in histories.items() if now - item.last_advertised_at > retention_time]
    for key in expired:
        del histories[key]
    return bool(expired)
