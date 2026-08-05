"""UI-independent monitoring engine that emits immutable state snapshots."""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
import threading
import time
from typing import Callable

from proxytools.checking import check_proxy, connection_string
from proxytools.sources.proxyscrape import fetch_all_proxies
from proxytools.stability import StabilityPolicy
from proxytools.stability.history import expire_histories, update_advertised


@dataclass(frozen=True, slots=True)
class MonitorRow:
    key: tuple[str, str]
    state: str
    alive_seconds: float
    checks: int
    required_checks: int
    streak: int
    success_rate: float
    median_latency: int | None
    p95_latency: int | None
    jitter: int
    country: str
    blockers: tuple[str, ...]
    connection: str


@dataclass(frozen=True, slots=True)
class MonitorSnapshot:
    cycle: int
    checked: int
    total: int
    stable_count: int
    tracked_count: int
    phase: str
    next_cycle_in: int | None
    rows: tuple[MonitorRow, ...]


class MonitorEngine:
    """Discover and check proxies continuously without depending on a UI."""

    def __init__(
        self,
        *,
        policy: StabilityPolicy,
        workers: int,
        timeout: float,
        samples: int,
        refresh_interval: float,
        retention_time: float,
        fetcher=fetch_all_proxies,
        checker=check_proxy,
    ):
        self.policy = policy
        self.workers = workers
        self.timeout = timeout
        self.samples = samples
        self.refresh_interval = refresh_interval
        self.retention_time = retention_time
        self.fetcher = fetcher
        self.checker = checker
        self.histories = {}
        self.cycle = 0
        self._refresh_requested = threading.Event()

    def request_refresh(self):
        self._refresh_requested.set()

    def snapshot(self, checked=0, total=0, phase="idle", next_cycle_in=None):
        now = time.monotonic()
        rows = []
        for history in self.histories.values():
            if history.latest is None:
                continue
            rows.append(
                MonitorRow(
                    key=history.key,
                    state=history.state,
                    alive_seconds=history.alive_for(now),
                    checks=len(history.samples),
                    required_checks=self.policy.config.min_checks,
                    streak=history.consecutive_successes,
                    success_rate=history.success_rate,
                    median_latency=history.median_latency,
                    p95_latency=history.p95_latency,
                    jitter=history.jitter,
                    country=history.latest.country,
                    blockers=tuple(self.policy.blockers(history, now)),
                    connection=connection_string(history.protocol, history.proxy),
                )
            )
        state_order = {"STABLE": 0, "PROBATION": 1, "DEGRADED": 2}
        rows.sort(
            key=lambda row: (
                state_order[row.state],
                -row.success_rate,
                row.p95_latency if row.p95_latency is not None else float("inf"),
                row.jitter,
            )
        )
        return MonitorSnapshot(
            cycle=self.cycle,
            checked=checked,
            total=total,
            stable_count=sum(history.state == "STABLE" for history in self.histories.values()),
            tracked_count=len(self.histories),
            phase=phase,
            next_cycle_in=next_cycle_in,
            rows=tuple(rows),
        )

    def run(self, stop: threading.Event, publish: Callable[[MonitorSnapshot], None]):
        while not stop.is_set():
            self._refresh_requested.clear()
            self.cycle += 1
            publish(self.snapshot(phase="fetching"))
            entries = self.fetcher()
            now = time.monotonic()
            update_advertised(self.histories, entries, now, self.policy.config.history_size)
            expire_histories(self.histories, now, self.retention_time)
            candidates = list(self.histories)
            checked = 0
            last_publish_at = 0.0
            publish(self.snapshot(checked, len(candidates), "checking"))

            executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.workers)
            pending = {
                executor.submit(self.checker, protocol, proxy, self.timeout, self.samples)
                for protocol, proxy in candidates
            }
            try:
                while pending and not stop.is_set():
                    done, pending = concurrent.futures.wait(
                        pending,
                        timeout=0.1,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in done:
                        result = future.result()
                        checked += 1
                        history = self.histories.get(result.key)
                        if history is not None:
                            history.record(result, time.monotonic(), self.policy)
                    publish_now = time.monotonic()
                    if done and (checked == len(candidates) or publish_now - last_publish_at >= 0.2):
                        publish(self.snapshot(checked, len(candidates), "checking"))
                        last_publish_at = publish_now
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            if stop.is_set():
                break

            deadline = time.monotonic() + self.refresh_interval
            previous_second = None
            while not stop.is_set() and not self._refresh_requested.is_set():
                remaining = max(0, deadline - time.monotonic())
                second = int(remaining)
                if second != previous_second:
                    publish(self.snapshot(checked, len(candidates), "waiting", second))
                    previous_second = second
                if remaining <= 0:
                    break
                stop.wait(min(0.2, remaining))
