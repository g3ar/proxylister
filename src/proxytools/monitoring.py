"""UI-independent monitoring engine that emits immutable state snapshots."""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
import threading
import time
from typing import Callable

from proxytools.checking import check_proxy, check_url, connection_string
from proxytools.sources.proxyscrape import fetch_all_proxies
from proxytools.stability import StabilityPolicy
from proxytools.stability.history import expire_histories, update_advertised
from proxytools.storage import CheckObservation


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
    first_seen_at: float | None = None
    total_observed_uptime: float = 0
    last_failure_at: float | None = None
    last_checked_at: float | None = None
    restored: bool = False


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
        repository=None,
        target_url=None,
    ):
        self.policy = policy
        self.workers = workers
        self.timeout = timeout
        self.samples = samples
        self.refresh_interval = refresh_interval
        self.retention_time = retention_time
        self.fetcher = fetcher
        self.checker = checker
        self.repository = repository
        self.target_url = target_url
        self.continuity_tolerance = 2 * refresh_interval
        self.histories = (
            repository.load_histories(policy, retention_time, self.continuity_tolerance)
            if repository is not None else {}
        )
        self._pending_observations = []
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
                    first_seen_at=history.first_seen_at,
                    total_observed_uptime=history.total_observed_uptime,
                    last_failure_at=history.last_failure_at,
                    last_checked_at=history.samples[-1].checked_at if history.samples else None,
                    restored=history.restored,
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
        self.cycle = 1
        saved_keys = set(self.histories)
        if saved_keys:
            self._check_candidates(
                list(self.histories), stop, publish, phase="restoring"
            )
        first_source_cycle = True
        while not stop.is_set():
            self._refresh_requested.clear()
            publish(self.snapshot(phase="fetching"))
            entries = self.fetcher()
            now = time.monotonic()
            update_advertised(self.histories, entries, now, self.policy.config.history_size)
            expire_histories(self.histories, now, self.retention_time)
            if first_source_cycle:
                candidates = [key for key in entries if key not in saved_keys]
                phase = "checking_new"
            else:
                candidates = list(self.histories)
                phase = "checking"
            checked = self._check_candidates(candidates, stop, publish, phase=phase)
            if self.repository is not None and self.cycle % 10 == 0:
                self.repository.prune_checks(time.time() - 86400)
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
            first_source_cycle = False
            self.cycle += 1

    def _check_candidates(self, candidates, stop, publish, *, phase):
        """Check one ordered candidate batch and publish incremental progress."""
        checked = 0
        last_publish_at = 0.0
        publish(self.snapshot(checked, len(candidates), phase))
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.workers)
        pending = {
            executor.submit(self._check_candidate, protocol, proxy)
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
                        self._record_result(history, result)
                publish_now = time.monotonic()
                if done and (checked == len(candidates) or publish_now - last_publish_at >= 0.2):
                    publish(self.snapshot(checked, len(candidates), phase))
                    last_publish_at = publish_now
        finally:
            # Running requests cannot be force-cancelled safely. During
            # shutdown wait here while the TUI still displays its message.
            executor.shutdown(wait=stop.is_set(), cancel_futures=True)
        self._flush()
        return checked

    def _check_candidate(self, protocol, proxy):
        """Run the normal proxy check and optional lightweight target request."""
        result = self.checker(protocol, proxy, self.timeout, self.samples)
        if result.ok and self.target_url and not check_url(result, self.target_url, self.timeout):
            result.ok = False
            result.failure_reason = "url"
        return result

    def _record_result(self, history, result):
        checked_mono = time.monotonic()
        checked_wall = time.time()
        old_state = history.state
        previous = history.samples[-1] if history.samples else None
        history.record(result, checked_mono, self.policy)
        accepted = history.samples[-1].ok
        if history.first_seen_at is None:
            history.first_seen_at = checked_wall
        if accepted and previous is not None and previous.ok:
            gap = checked_mono - previous.checked_at
            if 0 <= gap <= self.continuity_tolerance:
                history.total_observed_uptime += gap
        if not accepted:
            history.last_failure_at = checked_wall
        if self.repository is not None:
            reason = ", ".join(self.policy.blockers(history, checked_mono))
            self._pending_observations.append(
                CheckObservation(result, checked_wall, accepted, old_state, history.state, reason)
            )
            if len(self._pending_observations) >= 100:
                self._flush()

    def _flush(self):
        if self.repository is not None and self._pending_observations:
            pending, self._pending_observations = self._pending_observations, []
            self.repository.save_checks(pending, self.continuity_tolerance)
