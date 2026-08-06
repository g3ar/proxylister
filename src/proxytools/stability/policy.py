"""Configurable rules that decide when a proxy is stable."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from proxytools.stability.history import ProxyHistory


@dataclass(slots=True)
class StabilityConfig:
    history_size: int = 10
    min_checks: int = 5
    min_success_rate: float = 0.8
    min_success_streak: int = 3
    min_alive_time: float = 60
    max_latency: float = 500
    max_jitter: float = 500
    failure_tolerance: int = 2
    degraded_after: float = 60


class StabilityPolicy:
    def __init__(self, config: StabilityConfig):
        self.config = config

    def blockers(self, history: "ProxyHistory", now: float) -> list[str]:
        config = self.config
        reasons = []
        if not history.samples or not history.samples[-1].accepted:
            reasons.append(history.samples[-1].failure_reason if history.samples else "failed")
        if history.alive_for(now) < config.min_alive_time:
            reasons.append("alive")
        if len(history.samples) < config.min_checks:
            reasons.append("checks")
        if history.success_rate < config.min_success_rate:
            reasons.append("rate")
        if history.consecutive_successes < config.min_success_streak:
            reasons.append("streak")
        if history.median_latency is None or history.median_latency >= config.max_latency:
            reasons.append("latency")
        if history.jitter > config.max_jitter:
            reasons.append("jitter")
        return reasons

    def qualifies(self, history: "ProxyHistory", now: float) -> bool:
        return not self.blockers(history, now)
