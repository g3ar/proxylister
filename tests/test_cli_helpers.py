import csv
import json
from pathlib import Path
import tempfile
import unittest

from proxylib import ProxyResult
from proxylister import filter_and_sort, web_url, write_results
from proxymonitor import enforce_capacity, prune_stale


class CliHelperTests(unittest.TestCase):
    def setUp(self):
        self.fast = ProxyResult("http", "1.2.3.4:80", True, 50, "United States", 1.0, 2.0)
        self.slow = ProxyResult("socks5", "5.6.7.8:1080", True, 600, "Germany", 3.0, 4.0)

    def test_filter_and_sort(self):
        self.assertEqual(filter_and_sort([self.slow, self.fast], 500), [self.fast])

    def test_output_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            for output_format in ("text", "json", "csv"):
                path = Path(directory) / output_format
                write_results([self.fast], str(path), output_format)
                content = path.read_text(encoding="utf-8")
                self.assertIn("United States", content)
                if output_format == "json":
                    self.assertEqual(json.loads(content)["protocol"], "http")
                if output_format == "csv":
                    self.assertEqual(next(csv.DictReader(content.splitlines()))["proxy"], "1.2.3.4:80")

    def test_url_validation(self):
        self.assertEqual(web_url("https://example.com/a"), "https://example.com/a")
        with self.assertRaises(Exception):
            web_url("example.com")

    def test_monitor_capacity_and_stale_removal_use_protocol_key(self):
        other_protocol = ProxyResult("socks4", self.fast.proxy, True, 70)
        tracked = {self.fast.key: self.fast, other_protocol.key: other_protocol, self.slow.key: self.slow}
        self.assertTrue(enforce_capacity(tracked, 2))
        self.assertEqual(set(tracked), {self.fast.key, other_protocol.key})
        self.assertTrue(prune_stale(tracked, [self.fast.key]))
        self.assertEqual(set(tracked), {self.fast.key})


if __name__ == "__main__":
    unittest.main()
