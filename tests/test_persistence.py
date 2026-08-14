import tempfile
import unittest
from pathlib import Path

from proxylister.models import ProxyResult
from proxylister.process_lock import AlreadyRunning, ProcessLock
from proxylister.stability import StabilityConfig, StabilityPolicy
from proxylister.storage import CheckObservation, StateRepository
from proxylister.storage.sqlite import SCHEMA_VERSION


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "proxylister.db"
        self.repository = StateRepository(self.path)
        self.policy = StabilityPolicy(
            StabilityConfig(history_size=10, min_checks=2, min_success_streak=2, min_alive_time=5)
        )

    def tearDown(self):
        self.repository.close()
        self.temporary.cleanup()

    def test_checks_aggregates_and_transitions_are_saved(self):
        result = ProxyResult("http", "1.2.3.4:80", True, 42, "France")
        self.repository.save_checks(
            [
                CheckObservation(result, 100, True, "PROBATION", "PROBATION"),
                CheckObservation(result, 110, True, "PROBATION", "STABLE", "criteria met"),
            ],
            continuity_tolerance=20,
        )
        row = self.repository.connection.execute(
            "SELECT total_successes,total_observed_uptime,current_state FROM proxies"
        ).fetchone()
        self.assertEqual(row, (2, 10.0, "STABLE"))
        self.assertEqual(self.repository.connection.execute("SELECT count(*) FROM checks").fetchone()[0], 2)
        self.assertEqual(self.repository.connection.execute("SELECT count(*) FROM state_transitions").fetchone()[0], 1)

    def test_database_records_current_schema_version(self):
        version = self.repository.connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, SCHEMA_VERSION)

    def test_version_one_names_are_migrated_to_explicit_semantics(self):
        connection = self.repository.connection
        connection.execute(
            "ALTER TABLE proxies RENAME COLUMN last_check_accepted TO last_check_ok"
        )
        connection.execute("ALTER TABLE checks RENAME COLUMN accepted TO ok")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        self.repository.close()

        self.repository = StateRepository(self.path)

        proxy_columns = {
            row[1] for row in self.repository.connection.execute("PRAGMA table_info(proxies)")
        }
        check_columns = {
            row[1] for row in self.repository.connection.execute("PRAGMA table_info(checks)")
        }
        self.assertIn("last_check_accepted", proxy_columns)
        self.assertNotIn("last_check_ok", proxy_columns)
        self.assertIn("accepted", check_columns)
        self.assertNotIn("ok", check_columns)

    def test_reachability_and_acceptance_are_persisted_separately(self):
        result = ProxyResult(
            "http", "1.2.3.4:80", True, 125, "France", failure_reason="url"
        )
        self.repository.save_checks(
            [
                CheckObservation(
                    result, 100, False, "STABLE", "PROBATION", "url", 100
                )
            ],
            continuity_tolerance=20,
        )

        stored = self.repository.connection.execute(
            "SELECT accepted,reachable,latency_ms,failure_reason FROM checks"
        ).fetchone()
        self.assertEqual(stored, (0, 1, 125, "url"))
        history = self.repository.load_histories(
            self.policy, retention_time=1000, restart_tolerance=20,
            now_wall=110, now_mono=500,
        )[result.key]
        self.assertEqual(history.median_latency, 125)
        self.assertFalse(history.samples[-1].accepted)

    def test_degraded_proxy_and_its_history_are_removed(self):
        result = ProxyResult("http", "1.2.3.4:80", True, 42, "France")
        self.repository.save_checks(
            [CheckObservation(result, 100, True, "PROBATION", "STABLE")], 20
        )
        failed = ProxyResult("http", result.proxy, False)
        self.repository.save_checks(
            [CheckObservation(failed, 110, False, "STABLE", "DEGRADED")], 20
        )
        self.assertEqual(self.repository.connection.execute("SELECT count(*) FROM proxies").fetchone()[0], 0)
        self.assertEqual(self.repository.connection.execute("SELECT count(*) FROM checks").fetchone()[0], 0)
        self.assertEqual(self.repository.connection.execute("SELECT count(*) FROM state_transitions").fetchone()[0], 0)

    def test_never_working_probation_proxy_is_not_persisted(self):
        failed = ProxyResult("http", "1.2.3.4:80", False)
        self.repository.save_checks(
            [CheckObservation(failed, 100, False, "PROBATION", "PROBATION")], 20
        )
        self.assertEqual(self.repository.connection.execute("SELECT count(*) FROM proxies").fetchone()[0], 0)

    def test_stable_failure_grace_survives_restart(self):
        result = ProxyResult("http", "1.2.3.4:80", True, 42, "France")
        self.repository.save_checks(
            [CheckObservation(result, 100, True, "PROBATION", "STABLE")], 20
        )
        failed = ProxyResult("http", result.proxy, False)
        self.repository.save_checks(
            [CheckObservation(failed, 110, False, "STABLE", "PROBATION", "failed", 110)], 20
        )
        history = self.repository.load_histories(
            self.policy, retention_time=1000, restart_tolerance=20,
            now_wall=130, now_mono=500,
        )[result.key]
        self.assertEqual(history.state, "PROBATION")
        self.assertEqual(history.failure_since, 480)
        self.assertTrue(history.was_stable)
        self.assertTrue(history.restored)

    def test_tolerated_stable_failure_is_retained(self):
        result = ProxyResult("http", "1.2.3.4:80", True, 42, "France")
        failed = ProxyResult("http", result.proxy, False)
        self.repository.save_checks(
            [
                CheckObservation(result, 100, True, "PROBATION", "STABLE"),
                CheckObservation(failed, 110, False, "STABLE", "STABLE"),
            ],
            20,
        )

        stored = self.repository.connection.execute(
            "SELECT current_state,was_stable FROM proxies"
        ).fetchone()
        self.assertEqual(stored, ("STABLE", 1))

    def test_long_restart_gap_resets_alive_but_keeps_rolling_checks(self):
        result = ProxyResult("http", "1.2.3.4:80", True, 42, "France")
        self.repository.save_checks(
            [CheckObservation(result, 100, True, "PROBATION", "PROBATION")], 20
        )
        histories = self.repository.load_histories(
            self.policy, retention_time=1000, restart_tolerance=20, now_wall=200, now_mono=500
        )
        history = histories[result.key]
        self.assertEqual(len(history.samples), 1)
        self.assertIsNone(history.alive_since)
        self.assertEqual(history.state, "PROBATION")

    def test_last_known_status_is_restored_until_first_fresh_check(self):
        result = ProxyResult("http", "1.2.3.4:80", True, 42, "France")
        self.repository.save_checks(
            [CheckObservation(result, 100, True, "PROBATION", "STABLE")], 20
        )
        history = self.repository.load_histories(
            self.policy, retention_time=1, restart_tolerance=20, now_wall=1000, now_mono=500
        )[result.key]
        self.assertEqual(history.state, "STABLE")
        self.assertTrue(history.restored)
        self.assertIsNone(history.alive_since)

    def test_restored_stable_with_slow_median_is_normalized_to_probation(self):
        slow = ProxyResult("http", "1.2.3.4:80", True, 800, "France")
        self.repository.save_checks(
            [CheckObservation(slow, 100, False, "STABLE", "STABLE", was_stable=True)],
            20,
        )

        history = self.repository.load_histories(
            self.policy, retention_time=1000, restart_tolerance=20,
            now_wall=110, now_mono=500,
        )[slow.key]

        self.assertEqual(history.median_latency, 800)
        self.assertEqual(history.state, "PROBATION")
        self.assertTrue(history.was_stable)

    def test_slow_probation_from_former_stable_is_retained(self):
        fast = ProxyResult("http", "1.2.3.4:80", True, 42, "France")
        slow = ProxyResult("http", fast.proxy, True, 800, "France")
        self.repository.save_checks(
            [CheckObservation(fast, 100, True, "PROBATION", "STABLE", was_stable=True)],
            20,
        )
        self.repository.save_checks(
            [CheckObservation(slow, 110, False, "STABLE", "PROBATION", was_stable=True)],
            20,
        )

        stored = self.repository.connection.execute(
            "SELECT current_state,was_stable FROM proxies"
        ).fetchone()
        self.assertEqual(stored, ("PROBATION", 1))

    def test_short_restart_gap_preserves_continuity(self):
        result = ProxyResult("http", "1.2.3.4:80", True, 42, "France")
        self.repository.save_checks(
            [CheckObservation(result, 100, True, "PROBATION", "PROBATION")], 20
        )
        history = self.repository.load_histories(
            self.policy, retention_time=1000, restart_tolerance=20, now_wall=110, now_mono=500
        )[result.key]
        self.assertEqual(history.alive_for(500), 10)


class ProcessLockTests(unittest.TestCase):
    def test_second_process_lock_in_same_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxylister.lock"
            with ProcessLock("monitor", path):
                with self.assertRaises(AlreadyRunning):
                    with ProcessLock("list", path):
                        pass
            with ProcessLock("list", path):
                pass


if __name__ == "__main__":
    unittest.main()
