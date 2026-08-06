"""UI-independent monitoring engine that emits immutable state snapshots."""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
import threading
import time
from typing import Callable

from proxytools.checking import check_proxy, check_url, connection_string, probe_https_route
from proxytools.sources.proxyscrape import fetch_all_proxies
from proxytools.stability import StabilityPolicy
from proxytools.stability.history import update_advertised
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
    city: str = "Unknown"
    exit_ip: str = ""
    http_exit_ip: str = ""


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
    changed_rows: tuple[MonitorRow, ...] = ()
    incremental: bool = False
    active_checked: int = 0
    active_total: int = 0
    discovery_checked: int = 0
    discovery_total: int = 0
    resort: bool = False


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

    def snapshot(
        self, checked=0, total=0, phase="idle", next_cycle_in=None,
        changed_keys=None, *, active_checked=0, active_total=0,
        discovery_checked=0, discovery_total=0, resort=False,
    ):
        now = time.monotonic()
        changed_keys = set(changed_keys or ())
        incremental = bool(changed_keys) and (checked < total or phase == "running")
        histories = (
            (self.histories[key] for key in changed_keys if key in self.histories)
            if incremental else self.histories.values()
        )
        rows = []
        for history in histories:
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
                    city=history.latest.city,
                    exit_ip=history.latest.exit_ip,
                    http_exit_ip=history.latest.http_exit_ip,
                )
            )
        state_order = {"STABLE": 0, "PROBATION": 1, "DEGRADED": 2}
        rows.sort(
            key=lambda row: (
                state_order[row.state],
                row.median_latency if row.median_latency is not None else float("inf"),
            )
        )
        row_tuple = tuple(rows)
        return MonitorSnapshot(
            cycle=self.cycle,
            checked=checked,
            total=total,
            stable_count=sum(history.state == "STABLE" for history in self.histories.values()),
            tracked_count=len(self.histories),
            phase=phase,
            next_cycle_in=next_cycle_in,
            rows=row_tuple,
            changed_rows=row_tuple if incremental else (),
            incremental=incremental,
            active_checked=active_checked,
            active_total=active_total,
            discovery_checked=discovery_checked,
            discovery_total=discovery_total,
            resort=resort,
        )

    def run(self, stop: threading.Event, publish: Callable[[MonitorSnapshot], None]):
        self.cycle = 1
        active_workers = max(1, round(self.workers * 0.2))
        discovery_workers = max(1, self.workers - active_workers)
        active_pool = concurrent.futures.ThreadPoolExecutor(max_workers=active_workers)
        discovery_pool = concurrent.futures.ThreadPoolExecutor(max_workers=discovery_workers)
        source_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        active_pending = {}
        discovery_pending = {}
        source_future = source_pool.submit(self.fetcher)
        active_checked = active_total = discovery_checked = discovery_total = 0
        next_active_at = time.monotonic()
        next_source_at = float("inf")
        last_publish_at = 0.0
        last_flush_at = time.monotonic()
        dirty_keys = set()
        last_pruned_cycle = 0
        publish(self.snapshot(phase="running"))
        try:
            while not stop.is_set():
                now = time.monotonic()
                if self._refresh_requested.is_set():
                    self._refresh_requested.clear()
                    next_active_at = now
                    if source_future is None and not discovery_pending:
                        next_source_at = now
                if now >= next_active_at and not active_pending:
                    active_keys = [
                        key for key, history in self.histories.items()
                        if self._is_active(history) and key not in discovery_pending
                    ]
                    active_pending = {
                        active_pool.submit(self._check_candidate, *key): key for key in active_keys
                    }
                    active_checked, active_total = 0, len(active_keys)
                    next_active_at = now + self.refresh_interval

                if source_future is not None and source_future.done():
                    entries = source_future.result()
                    source_future = None
                    advertised_at = time.monotonic()
                    update_advertised(
                        self.histories, entries, advertised_at, self.policy.config.history_size
                    )
                    self._expire_inactive(advertised_at)
                    active_keys = {
                        key for key, history in self.histories.items() if self._is_active(history)
                    }
                    candidates = [
                        key for key in entries
                        if key not in active_keys
                        and key not in active_pending.values()
                        and key not in discovery_pending.values()
                    ]
                    discovery_pending = {
                        discovery_pool.submit(self._check_candidate, *key): key for key in candidates
                    }
                    discovery_checked, discovery_total = 0, len(candidates)
                    if not candidates:
                        next_source_at = advertised_at + self.refresh_interval

                if (
                    source_future is None and not discovery_pending
                    and time.monotonic() >= next_source_at
                ):
                    source_future = source_pool.submit(self.fetcher)
                    next_source_at = float("inf")
                    self.cycle += 1

                all_pending = set(active_pending) | set(discovery_pending)
                done = set()
                if all_pending:
                    done, _ = concurrent.futures.wait(
                        all_pending, timeout=0.1,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                else:
                    stop.wait(0.1)

                resort = False
                for future in done:
                    if future in active_pending:
                        key = active_pending.pop(future)
                        active_checked += 1
                        lane_finished = not active_pending
                    else:
                        key = discovery_pending.pop(future)
                        discovery_checked += 1
                        lane_finished = not discovery_pending
                        if lane_finished:
                            next_source_at = time.monotonic() + self.refresh_interval
                    result = future.result()
                    history = self.histories.get(key)
                    if history is not None:
                        self._record_result(history, result)
                        dirty_keys.add(key)
                    resort = resort or lane_finished

                publish_now = time.monotonic()
                if dirty_keys and (resort or publish_now - last_publish_at >= 0.2):
                    publish(self.snapshot(
                        active_checked + discovery_checked,
                        active_total + discovery_total,
                        "running",
                        changed_keys=dirty_keys,
                        active_checked=active_checked,
                        active_total=active_total,
                        discovery_checked=discovery_checked,
                        discovery_total=discovery_total,
                        resort=resort,
                    ))
                    dirty_keys.clear()
                    last_publish_at = publish_now
                if publish_now - last_flush_at >= 1:
                    self._flush()
                    last_flush_at = publish_now
                if (
                    self.repository is not None and self.cycle % 10 == 0
                    and self.cycle != last_pruned_cycle
                ):
                    self.repository.prune_checks(time.time() - 86400)
                    last_pruned_cycle = self.cycle
        finally:
            active_pool.shutdown(wait=True, cancel_futures=True)
            discovery_pool.shutdown(wait=True, cancel_futures=True)
            source_pool.shutdown(wait=True, cancel_futures=True)
            self._flush()

    @staticmethod
    def _is_active(history):
        return (
            history.state in {"STABLE", "PROBATION"}
            and history.latest is not None
            and (
                history.restored
                or history.failure_since is not None
                or (history.samples and history.samples[-1].ok)
            )
        )

    def _expire_inactive(self, now):
        expired = [
            key for key, history in self.histories.items()
            if not self._is_active(history)
            and now - history.last_advertised_at > self.retention_time
        ]
        for key in expired:
            del self.histories[key]

    def _check_candidate(self, protocol, proxy):
        """Run the normal proxy check and optional lightweight target request."""
        result = self.checker(protocol, proxy, self.timeout, self.samples)
        if result.ok and self.target_url:
            probe_https_route(result, self.timeout)
            if not check_url(
                result, self.target_url, self.timeout, accept_forbidden=True
            ):
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
            failure_since = (
                checked_wall - (checked_mono - history.failure_since)
                if history.failure_since is not None else None
            )
            self._pending_observations.append(
                CheckObservation(
                    result, checked_wall, accepted, old_state, history.state,
                    reason, failure_since,
                )
            )
            if len(self._pending_observations) >= 100:
                self._flush()

    def _flush(self):
        if self.repository is not None and self._pending_observations:
            pending, self._pending_observations = self._pending_observations, []
            self.repository.save_checks(pending, self.continuity_tolerance)
