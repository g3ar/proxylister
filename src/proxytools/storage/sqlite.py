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

SCHEMA_VERSION = 2


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
        self._migrate_schema()
        self._discard_unusable_proxies()

    def _create_schema(self):
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY,
                protocol TEXT NOT NULL,
                address TEXT NOT NULL,
                country TEXT NOT NULL DEFAULT 'Unknown',
                city TEXT NOT NULL DEFAULT 'Unknown',
                exit_ip TEXT NOT NULL DEFAULT '',
                lat REAL, lon REAL,
                first_seen_at REAL NOT NULL,
                last_seen_at REAL NOT NULL,
                last_checked_at REAL,
                last_success_at REAL,
                last_failure_at REAL,
                last_check_accepted INTEGER,
                total_successes INTEGER NOT NULL DEFAULT 0,
                total_failures INTEGER NOT NULL DEFAULT 0,
                total_observed_uptime REAL NOT NULL DEFAULT 0,
                current_state TEXT NOT NULL DEFAULT 'PROBATION',
                failure_since REAL,
                was_stable INTEGER NOT NULL DEFAULT 0,
                UNIQUE(protocol, address)
            );
            CREATE TABLE IF NOT EXISTS checks (
                id INTEGER PRIMARY KEY,
                proxy_id INTEGER NOT NULL REFERENCES proxies(id) ON DELETE CASCADE,
                checked_at REAL NOT NULL,
                accepted INTEGER NOT NULL,
                reachable INTEGER NOT NULL,
                latency_ms INTEGER,
                failure_reason TEXT NOT NULL DEFAULT '',
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
                retained_failure = item.new_state == "STABLE" or (
                    item.new_state == "PROBATION" and item.failure_since is not None
                )
                if (
                    item.new_state not in {"STABLE", "PROBATION"}
                    or (not item.accepted and not retained_failure)
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
                    "SELECT id, last_checked_at, last_check_accepted FROM proxies WHERE protocol=? AND address=?",
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
                        protocol,address,country,city,exit_ip,lat,lon,first_seen_at,last_seen_at,last_checked_at,
                        last_success_at,last_failure_at,last_check_accepted,total_successes,total_failures,
                        total_observed_uptime,current_state,failure_since,was_stable
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(protocol,address) DO UPDATE SET
                        country=CASE WHEN excluded.country != 'Unknown' THEN excluded.country ELSE proxies.country END,
                        city=CASE WHEN excluded.city != 'Unknown' THEN excluded.city ELSE proxies.city END,
                        exit_ip=CASE WHEN excluded.exit_ip != '' THEN excluded.exit_ip ELSE proxies.exit_ip END,
                        lat=COALESCE(excluded.lat,proxies.lat), lon=COALESCE(excluded.lon,proxies.lon),
                        last_seen_at=excluded.last_seen_at, last_checked_at=excluded.last_checked_at,
                        last_success_at=COALESCE(excluded.last_success_at,proxies.last_success_at),
                        last_failure_at=COALESCE(excluded.last_failure_at,proxies.last_failure_at),
                        last_check_accepted=excluded.last_check_accepted,
                        total_successes=proxies.total_successes+excluded.total_successes,
                        total_failures=proxies.total_failures+excluded.total_failures,
                        total_observed_uptime=proxies.total_observed_uptime+excluded.total_observed_uptime,
                        current_state=excluded.current_state,
                        failure_since=excluded.failure_since,
                        was_stable=MAX(proxies.was_stable,excluded.was_stable)
                    """,
                    (result.protocol, result.proxy, result.country, result.city,
                     result.exit_ip, result.lat, result.lon,
                     item.checked_at, item.checked_at, item.checked_at, success_at, failure_at,
                     int(item.accepted), int(item.accepted), int(not item.accepted), uptime,
                     item.new_state, item.failure_since, int(item.new_state == "STABLE")),
                )
                proxy_id = self.connection.execute(
                    "SELECT id FROM proxies WHERE protocol=? AND address=?", result.key
                ).fetchone()[0]
                self.connection.execute(
                    """INSERT INTO checks(
                           proxy_id,checked_at,accepted,reachable,latency_ms,
                           failure_reason,country,lat,lon
                       ) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (proxy_id, item.checked_at, int(item.accepted),
                     int(result.reachable and result.latency_ms is not None),
                     result.latency_ms, result.failure_reason, result.country,
                     result.lat, result.lon),
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
                      OR (last_check_accepted IS NOT 1 AND failure_since IS NULL
                          AND current_state != 'STABLE')"""
            )

    def _migrate_schema(self):
        """Upgrade older clone databases in explicit, ordered steps."""
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {version} is newer than supported {SCHEMA_VERSION}"
            )
        if version < 1:
            # Version 1 consolidated additive columns from pre-versioned builds.
            columns = {
                row[1] for row in self.connection.execute("PRAGMA table_info(proxies)")
            }
            additions = {
                "failure_since": "REAL",
                "city": "TEXT NOT NULL DEFAULT 'Unknown'",
                "exit_ip": "TEXT NOT NULL DEFAULT ''",
                "was_stable": "INTEGER NOT NULL DEFAULT 0",
            }
            with self.connection:
                for name, definition in additions.items():
                    if name not in columns:
                        self.connection.execute(
                            f"ALTER TABLE proxies ADD COLUMN {name} {definition}"
                        )
                check_columns = {
                    row[1] for row in self.connection.execute("PRAGMA table_info(checks)")
                }
                if "reachable" not in check_columns:
                    self.connection.execute("ALTER TABLE checks ADD COLUMN reachable INTEGER")
                self.connection.execute(
                    """UPDATE proxies SET was_stable=1
                       WHERE current_state='STABLE' OR EXISTS (
                           SELECT 1 FROM state_transitions
                           WHERE state_transitions.proxy_id=proxies.id
                             AND state_transitions.new_state='STABLE'
                       )"""
                )
                self.connection.execute("PRAGMA user_version=1")
            version = 1

        if version < 2:
            # Version 2 names reachability and complete acceptance separately.
            columns = {
                row[1] for row in self.connection.execute("PRAGMA table_info(proxies)")
            }
            check_columns = {
                row[1] for row in self.connection.execute("PRAGMA table_info(checks)")
            }
            with self.connection:
                if "last_check_ok" in columns and "last_check_accepted" not in columns:
                    self.connection.execute(
                        "ALTER TABLE proxies RENAME COLUMN last_check_ok TO last_check_accepted"
                    )
                if "ok" in check_columns and "accepted" not in check_columns:
                    self.connection.execute(
                        "ALTER TABLE checks RENAME COLUMN ok TO accepted"
                    )
                if "failure_reason" not in check_columns:
                    self.connection.execute(
                        "ALTER TABLE checks ADD COLUMN failure_reason TEXT NOT NULL DEFAULT ''"
                    )
                self.connection.execute("PRAGMA user_version=2")

    def load_histories(self, policy, retention_time: float, restart_tolerance: float, *, now_wall=None, now_mono=None):
        """Rebuild recent rolling histories without counting a long offline gap."""
        now_wall = time.time() if now_wall is None else now_wall
        now_mono = time.monotonic() if now_mono is None else now_mono
        histories = {}
        proxies = self.connection.execute(
            """SELECT id,protocol,address,country,city,exit_ip,lat,lon,first_seen_at,total_observed_uptime,
                      last_failure_at,last_checked_at,last_check_accepted,current_state,failure_since,was_stable
               FROM proxies ORDER BY
                   CASE current_state WHEN 'STABLE' THEN 0 WHEN 'DEGRADED' THEN 1 ELSE 2 END,
                   last_checked_at DESC"""
        ).fetchall()
        for (proxy_id, protocol, address, country, city, exit_ip, lat, lon,
             first_seen, uptime,
             last_failure, last_checked, _last_check_accepted, stored_state, failure_since,
             was_stable) in proxies:
            history = ProxyHistory(protocol, address, policy.config.history_size)
            history.first_seen_at = first_seen
            history.total_observed_uptime = uptime
            history.last_failure_at = last_failure
            history.was_stable = bool(was_stable)
            samples = self.connection.execute(
                """SELECT checked_at,accepted,reachable,latency_ms,failure_reason,country,lat,lon FROM (
                       SELECT checked_at,accepted,reachable,latency_ms,failure_reason,country,lat,lon FROM checks
                       WHERE proxy_id=? ORDER BY checked_at DESC LIMIT ?
                   ) ORDER BY checked_at""",
                (proxy_id, policy.config.history_size),
            ).fetchall()
            for checked_at, _accepted, reachable, latency, failure_reason, country, lat, lon in samples:
                synthetic_now = now_mono - max(0, now_wall - checked_at)
                result = ProxyResult(
                    protocol, address, bool(reachable), latency, country, lat, lon,
                    failure_reason=failure_reason,
                )
                history.record(result, synthetic_now, policy)
            if history.latest is None:
                history.latest = ProxyResult(
                    protocol, address, False, None, country, lat, lon,
                    city=city, exit_ip=exit_ip,
                )
            else:
                history.latest.city = city
                history.latest.exit_ip = exit_ip
            history.last_advertised_at = now_mono
            if last_checked is None or now_wall - last_checked > restart_tolerance:
                history.alive_since = None
                history.stable_since = None
            if failure_since is not None:
                history.failure_since = now_mono - max(0, now_wall - failure_since)
            history.state = stored_state
            history.was_stable = bool(
                history.was_stable or was_stable or stored_state == "STABLE"
            )
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
