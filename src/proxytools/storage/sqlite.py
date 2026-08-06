"""SQLite persistence for proxy checks, aggregates, and state transitions.

``StateRepository`` owns the schema and writes a complete batch in one
transaction. Recent detailed checks rebuild the rolling in-memory histories on
startup; compact lifetime aggregates remain available after those details are
pruned. Timestamps on disk are Unix UTC seconds, while the live engine keeps
using monotonic time for duration calculations.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time

from proxytools.models import ProxyResult
from proxytools.stability.history import ProxyHistory


@dataclass(frozen=True, slots=True)
class CheckObservation:
    result: ProxyResult
    checked_at: float
    accepted: bool
    old_state: str
    new_state: str
    reason: str = ""
    failure_since: float | None = None


class StateRepository:
    """Read and update one clone's monitor state database."""

    def __init__(self, path: Path):
        self.path = path
        # Textual runs the monitor engine in its worker thread; the repository
        # remains single-writer but must be usable from that thread.
        self.connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._create_schema()
        self._ensure_runtime_columns()
        self._discard_unusable_proxies()

    def _create_schema(self):
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY,
                protocol TEXT NOT NULL,
                address TEXT NOT NULL,
                country TEXT NOT NULL DEFAULT 'Unknown',
                lat REAL, lon REAL,
                first_seen_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                last_checked_at REAL,
                last_success_at REAL,
                last_failure_at REAL,
                last_check_ok INTEGER,
                total_successes INTEGER NOT NULL DEFAULT 0,
                total_failures INTEGER NOT NULL DEFAULT 0,
                total_observed_uptime REAL NOT NULL DEFAULT 0,
                current_state TEXT NOT NULL DEFAULT 'PROBATION',
                failure_since REAL,
                UNIQUE(protocol, address)
            );
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY,
                proxy_id INTEGER NOT NULL REFERENCES proxies(id) ON DELETE CASCADE,
                checked_at REAL NOT NULL,
                ok INTEGER NOT NULL,
                latency_ms INTEGER,
                country TEXT NOT NULL DEFAULT 'Unknown',
                lat REAL, lon REAL
            );
            CREATE INDEX IF NOT EXISTS checks_proxy_time ON checks(proxy_id, checked_at DESC);
            CREATE INDEX IF NOT EXISTS checks_time ON checks(checked_at);
            CREATE TABLE IF NOT EXISTS state_transitions (
                id INTEGER PRIMARY KEY,
                proxy_id INTEGER NOT NULL REFERENCES proxies(id) ON DELETE CASCADE,
                changed_at REAL NOT NULL,
                old_state TEXT NOT NULL,
                new_state TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS transitions_proxy_time
                ON state_transitions(proxy_id, changed_at DESC);
            """
        )
        self.connection.commit()

    def save_checks(self, observations, continuity_tolerance: float):
        """Persist completed checks and lifetime counters in one transaction."""
        with self.connection:
            for item in observations:
                result = item.result
                grace_probation = (
                    item.new_state == "PROBATION" and item.failure_since is not None
                )
                if (
                    item.new_state not in {"STABLE", "PROBATION"}
                    or (not item.accepted and not grace_probation)
                ):
                    # SQLite is restart state, not a graveyard. Foreign keys
                    # cascade detailed checks and transitions for a proxy that
                    # is no longer usable; its in-memory history may still
                    # recover and be inserted again later in this process.
                    self.connection.execute(
                        "DELETE FROM proxies WHERE protocol=? AND address=?",
                        result.key,
                    )
                    continue
                previous = self.connection.execute(
                    "SELECT id, last_checked_at, last_check_ok FROM proxies WHERE protocol=? AND address=?",
                    result.key,
                ).fetchone()
                uptime = 0.0
                if previous and item.accepted and previous[2] and previous[1] is not None:
                    gap = item.checked_at - previous[1]
                    if 0 <= gap <= continuity_tolerance:
                        uptime = gap
                success_at = item.checked_at if item.accepted else None
                failure_at = None if item.accepted else item.checked_at
                self.connection.execute(
                    """
                    INSERT INTO proxies (
                        protocol,address,country,lat,lon,first_seen_at,last_seen_at,last_checked_at,
                        last_success_at,last_failure_at,last_check_ok,total_successes,total_failures,
                        total_observed_uptime,current_state,failure_since
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(protocol,address) DO UPDATE SET
                        country=CASE WHEN excluded.country != 'Unknown' THEN excluded.country ELSE proxies.country END,
                        lat=COALESCE(excluded.lat,proxies.lat), lon=COALESCE(excluded.lon,proxies.lon),
                        last_seen_at=excluded.last_seen_at, last_checked_at=excluded.last_checked_at,
                        last_success_at=COALESCE(excluded.last_success_at,proxies.last_success_at),
                        last_failure_at=COALESCE(excluded.last_failure_at,proxies.last_failure_at),
                        last_check_ok=excluded.last_check_ok,
                        total_successes=proxies.total_successes+excluded.total_successes,
                        total_failures=proxies.total_failures+excluded.total_failures,
                        total_observed_uptime=proxies.total_observed_uptime+excluded.total_observed_uptime,
                        current_state=excluded.current_state,
                        failure_since=excluded.failure_since
                    """,
                    (result.protocol, result.proxy, result.country, result.lat, result.lon,
                     item.checked_at, item.checked_at, item.checked_at, success_at, failure_at,
                     int(item.accepted), int(item.accepted), int(not item.accepted), uptime,
                     item.new_state, item.failure_since),
                )
                proxy_id = self.connection.execute(
                    "SELECT id FROM proxies WHERE protocol=? AND address=?", result.key
                ).fetchone()[0]
                self.connection.execute(
                    "INSERT INTO checks(proxy_id,checked_at,ok,latency_ms,country,lat,lon) VALUES(?,?,?,?,?,?,?)",
                    (proxy_id, item.checked_at, int(item.accepted), result.latency_ms if item.accepted else None,
                     result.country, result.lat, result.lon),
                )
                if item.old_state != item.new_state:
                    self.connection.execute(
                        "INSERT INTO state_transitions(proxy_id,changed_at,old_state,new_state,reason) VALUES(?,?,?,?,?)",
                        (proxy_id, item.checked_at, item.old_state, item.new_state, item.reason),
                    )

    def _discard_unusable_proxies(self):
        """Remove stale records written by versions that retained failures."""
        with self.connection:
            self.connection.execute(
                """DELETE FROM proxies
                   WHERE current_state NOT IN ('STABLE','PROBATION')
                      OR (last_check_ok IS NOT 1 AND failure_since IS NULL)"""
            )

    def _ensure_runtime_columns(self):
        """Apply additive schema upgrades to databases from older clones."""
        columns = {
            row[1] for row in self.connection.execute("PRAGMA table_info(proxies)")
        }
        if "failure_since" not in columns:
            with self.connection:
                self.connection.execute("ALTER TABLE proxies ADD COLUMN failure_since REAL")

    def load_histories(self, policy, retention_time: float, restart_tolerance: float, *, now_wall=None, now_mono=None):
        """Rebuild recent rolling histories without counting a long offline gap."""
        now_wall = time.time() if now_wall is None else now_wall
        now_mono = time.monotonic() if now_mono is None else now_mono
        histories = {}
        proxies = self.connection.execute(
            """SELECT id,protocol,address,country,lat,lon,first_seen_at,total_observed_uptime,
                      last_failure_at,last_checked_at,last_check_ok,current_state,failure_since
               FROM proxies ORDER BY
                   CASE current_state WHEN 'STABLE' THEN 0 WHEN 'DEGRADED' THEN 1 ELSE 2 END,
                   last_checked_at DESC"""
        ).fetchall()
        for (proxy_id, protocol, address, country, lat, lon, first_seen, uptime,
             last_failure, last_checked, last_check_ok, stored_state, failure_since) in proxies:
            history = ProxyHistory(protocol, address, policy.config.history_size)
            history.first_seen_at = first_seen
            history.total_observed_uptime = uptime
            history.last_failure_at = last_failure
            samples = self.connection.execute(
                """SELECT checked_at,ok,latency_ms,country,lat,lon FROM (
                       SELECT checked_at,ok,latency_ms,country,lat,lon FROM checks
                       WHERE proxy_id=? ORDER BY checked_at DESC LIMIT ?
                   ) ORDER BY checked_at""",
                (proxy_id, policy.config.history_size),
            ).fetchall()
            for checked_at, ok, latency, country, lat, lon in samples:
                synthetic_now = now_mono - max(0, now_wall - checked_at)
                result = ProxyResult(protocol, address, bool(ok), latency, country, lat, lon)
                history.record(result, synthetic_now, policy)
            if history.latest is None:
                history.latest = ProxyResult(
                    protocol, address, bool(last_check_ok), None, country, lat, lon
                )
            history.last_advertised_at = now_mono
            if last_checked is None or now_wall - last_checked > restart_tolerance:
                history.alive_since = None
                history.stable_since = None
            if failure_since is not None:
                history.failure_since = now_mono - max(0, now_wall - failure_since)
            history.state = stored_state
            history.restored = True
            histories[history.key] = history
        return histories

    def prune_checks(self, older_than: float):
        with self.connection:
            self.connection.execute("DELETE FROM checks WHERE checked_at < ?", (older_than,))

    def reset(self):
        with self.connection:
            self.connection.execute("DELETE FROM state_transitions")
            self.connection.execute("DELETE FROM checks")
            self.connection.execute("DELETE FROM proxies")

    def close(self):
        self.connection.close()
