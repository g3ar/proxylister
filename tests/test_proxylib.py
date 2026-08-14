import argparse
import contextlib
import io
import os
from pathlib import Path
import signal
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from proxylister import config
from proxylister import http
from proxylister.commands import list as list_command
from proxylister.checking import proxy as checker
from proxylister.models import ProxyResult
from proxylister.output.results import write_proxy_file
from proxylister.sources import proxyscrape


class FakeResponse:
    def __init__(self, text="", status_code=200, payload=None):
        self.text = text
        self.status_code = status_code
        self._payload = payload or {}
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError()

    def json(self):
        return self._payload

    def close(self):
        self.closed = True


class ProxyLibraryTests(unittest.TestCase):
    def test_list_defers_sigint_until_progress_cleanup_and_saves_partial_results(self):
        class FragileProgress:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, _exc, _traceback):
                if exc_type is KeyboardInterrupt:
                    raise RuntimeError("release unlocked lock")

            def add_task(self, *_args, **_kwargs):
                return 1

            def update(self, *_args, **_kwargs):
                handler = signal.getsignal(signal.SIGINT)
                handler(signal.SIGINT, None)

        settings = Mock(url=None, max_latency=500, workers=1, timeout=1, samples=1)
        result = ProxyResult("http", "192.0.2.1:80", True, 50)
        with patch.object(list_command, "load_config", return_value=settings), patch.object(
            list_command, "fetch_all_proxies", return_value=[("http", result.proxy)]
        ), patch.object(
            list_command, "check_candidate", return_value=result
        ), patch.object(
            list_command, "progress_display", return_value=FragileProgress()
        ), patch.object(
            list_command, "write_proxy_file", return_value=(Path("working_proxies.txt"), 1)
        ) as writer, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(list_command.main([]), 0)

        writer.assert_called_once_with([result])

    def test_proxy_file_atomically_replaces_stale_plain_results(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "proxylister.output.results.working_proxies_path",
            return_value=Path(directory) / "working_proxies.txt",
        ):
            path = Path(directory) / "working_proxies.txt"
            path.write_text("stale\n")
            results = [
                ProxyResult("socks5", "198.51.100.2:1080", True, 20),
                ProxyResult("http", "192.0.2.1:80", True, 10),
            ]
            written, count = write_proxy_file(results)

            self.assertEqual((written, count), (path, 2))
            self.assertEqual(
                path.read_text(),
                "socks5://198.51.100.2:1080\nhttp://192.0.2.1:80\n",
            )
            self.assertFalse((Path(directory) / ".working_proxies.txt.tmp").exists())

    def test_fetch_proxy_list_extracts_and_dedupes(self):
        response = FakeResponse("1.2.3.4:80\ninvalid\n1.2.3.4:80\n5.6.7.8:1080")
        with patch.object(proxyscrape, "session") as session:
            session.return_value.get.return_value = response
            self.assertEqual(proxyscrape.fetch_proxy_list("http"), ["1.2.3.4:80", "5.6.7.8:1080"])
        self.assertTrue(response.closed)

    def test_all_protocols_for_same_address_are_preserved(self):
        lists = {"http": ["1.2.3.4:80"], "socks4": ["1.2.3.4:80"], "socks5": []}
        with patch.object(proxyscrape, "fetch_proxy_list", side_effect=lambda protocol: lists[protocol]):
            self.assertEqual(
                proxyscrape.fetch_all_proxies(),
                [("http", "1.2.3.4:80"), ("socks4", "1.2.3.4:80")],
            )

    def test_complete_source_failure_is_distinct_from_an_empty_list(self):
        with patch.object(
            proxyscrape, "fetch_proxy_list", side_effect=requests.ConnectionError("offline")
        ), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(proxyscrape.ProxySourceUnavailable):
                proxyscrape.fetch_all_proxies()

    def test_list_returns_failure_when_proxy_source_is_unavailable(self):
        with patch.object(
            list_command, "fetch_all_proxies",
            side_effect=proxyscrape.ProxySourceUnavailable("offline"),
        ), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(list_command.main([]), 1)

    def test_empty_successful_list_clears_the_saved_proxy_file(self):
        settings = Mock(url=None, max_latency=500, workers=1, timeout=1, samples=1)
        with patch.object(list_command, "load_config", return_value=settings), patch.object(
            list_command, "fetch_all_proxies", return_value=[]
        ), patch.object(
            list_command, "write_proxy_file", return_value=(Path("working_proxies.txt"), 0)
        ) as writer, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(list_command.main([]), 0)

        writer.assert_called_once_with([])

    def test_browser_validation_submits_only_one_check_at_a_time(self):
        first_browser_started = threading.Event()
        release_browser = threading.Event()
        browser_submissions = 0
        real_executor = list_command.concurrent.futures.ThreadPoolExecutor

        class TrackingBrowserExecutor:
            def __init__(self, max_workers):
                nonlocal browser_submissions
                self.executor = real_executor(max_workers=max_workers)
                self.is_browser = max_workers == 1

            def submit(self, function, *args):
                nonlocal browser_submissions
                if self.is_browser:
                    browser_submissions += 1
                return self.executor.submit(function, *args)

            def shutdown(self, **kwargs):
                return self.executor.shutdown(**kwargs)

        settings = Mock(
            url=None, max_latency=500, workers=2, timeout=1, samples=1
        )
        candidates = [("http", f"192.0.2.{index}:80") for index in range(1, 4)]

        def browser_check(result, *_args):
            first_browser_started.set()
            release_browser.wait(2)
            return result

        with patch.object(list_command, "load_config", return_value=settings), patch.object(
            list_command, "fetch_all_proxies", return_value=candidates
        ), patch.object(
            list_command, "check_candidate",
            side_effect=lambda protocol, proxy, *_args: ProxyResult(
                protocol, proxy, True, 50
            ),
        ), patch.object(
            list_command, "browser_check", side_effect=browser_check
        ), patch.object(
            list_command.concurrent.futures, "ThreadPoolExecutor", TrackingBrowserExecutor
        ), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            runner = threading.Thread(
                target=list_command.main,
                args=(["--url", "https://example.com", "--browser-check", "--headless"],),
            )
            runner.start()
            self.assertTrue(first_browser_started.wait(1))
            self.assertEqual(browser_submissions, 1)
            release_browser.set()
            runner.join(3)

        self.assertFalse(runner.is_alive())
        self.assertEqual(browser_submissions, len(candidates))

    def test_check_proxy_uses_median_complete_duration(self):
        response = FakeResponse(payload={"ip": "203.0.113.20"})
        with patch.object(checker, "proxy_session") as proxy_session, patch.object(
            checker, "locate", return_value={
                "country": "Germany", "city": "Nuremberg", "lat": 49.45, "lon": 11.08,
            }
        ), patch.object(
            checker.time, "perf_counter", side_effect=[0.0, 0.3, 1.0, 1.1, 2.0, 2.2]
        ):
            proxy_session.return_value.__enter__.return_value.get.return_value = response
            result = checker.check_proxy("http", "1.2.3.4:80", samples=3)
        self.assertTrue(result.reachable)
        self.assertEqual(result.latency_ms, 200)
        self.assertEqual(result.exit_ip, "203.0.113.20")
        self.assertEqual((result.country, result.city), ("Germany", "Nuremberg"))

    def test_monitor_url_check_accepts_antibot_403_but_not_404(self):
        result = ProxyResult("http", "1.2.3.4:80", True, 50)
        with patch.object(checker, "proxy_session") as proxy_session:
            response = proxy_session.return_value.__enter__.return_value.get.return_value
            response.status_code = 403
            self.assertFalse(checker.check_url(result, "https://example.com", 3))
            self.assertTrue(
                checker.check_url(
                    result, "https://example.com", 3, accept_forbidden=True
                )
            )
            response.status_code = 404
            self.assertFalse(
                checker.check_url(
                    result, "https://example.com", 3, accept_forbidden=True
                )
            )

    def test_proxy_session_closes_all_cached_proxy_connections(self):
        with patch.object(http.requests, "Session") as session_factory:
            with http.proxy_session() as current:
                self.assertIs(current, session_factory.return_value)
            session_factory.return_value.close.assert_called_once_with()

    @unittest.skipUnless(Path("/proc/self/fd").is_dir(), "requires Linux /proc descriptor accounting")
    def test_repeated_proxy_checks_do_not_accumulate_descriptors(self):
        class DescriptorSession:
            def __init__(self):
                self.read_fd, self.write_fd = os.pipe()

            def get(self, *_args, **_kwargs):
                return FakeResponse(payload={"ip": "203.0.113.20"})

            def close(self):
                os.close(self.read_fd)
                os.close(self.write_fd)

        baseline = len(os.listdir("/proc/self/fd"))
        location = {"country": "France", "city": "Paris", "lat": 1, "lon": 2}
        with patch.object(http.requests, "Session", side_effect=DescriptorSession), patch.object(
            checker, "locate", return_value=location
        ):
            for index in range(250):
                result = checker.check_proxy("http", f"192.0.2.{index % 250}:8080")
                self.assertTrue(result.reachable)

        self.assertLessEqual(len(os.listdir("/proc/self/fd")), baseline + 1)

    def test_list_candidate_uses_identity_result_and_lightweight_url_check(self):
        result = ProxyResult("http", "1.2.3.4:80", True, 50)
        with patch.object(list_command, "check_proxy", return_value=result) as identity, patch.object(
            list_command, "check_url", return_value=True
        ) as target:
            checked = list_command.check_candidate(
                "http", "1.2.3.4:80", 3, 1, "https://example.com"
            )
        self.assertIs(checked, result)
        identity.assert_called_once_with("http", "1.2.3.4:80", 3, 1)
        target.assert_called_once_with(
            result, "https://example.com", 3, accept_forbidden=True
        )

    def test_argument_validators_reject_bad_values(self):
        for value in ("0", "-1"):
            with self.assertRaises(argparse.ArgumentTypeError):
                config.positive_float(value)
        for value in ("0", "101"):
            with self.assertRaises(argparse.ArgumentTypeError):
                config.worker_count(value)
        for value in ("0", "6"):
            with self.assertRaises(argparse.ArgumentTypeError):
                config.sample_count(value)


if __name__ == "__main__":
    unittest.main()
