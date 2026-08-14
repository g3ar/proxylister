from pathlib import Path
import tempfile
import unittest

from proxylister.config import ConfigError, load_config


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.source = Path(__file__).parents[1] / "proxylister.conf"

    def _load_text(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxylister.conf"
            path.write_text(text, encoding="utf-8")
            return load_config(path)

    def test_project_config_loads_all_typed_values(self):
        settings = load_config(self.source)
        self.assertEqual(settings.workers, 50)
        self.assertEqual(settings.max_latency, 500)
        self.assertEqual(settings.browser, "auto")
        self.assertIsNone(settings.url)

    def test_inline_comments_are_supported(self):
        text = self.source.read_text(encoding="utf-8").replace(
            "WORKERS=50", "WORKERS=25  # temporary host override"
        )
        self.assertEqual(self._load_text(text).workers, 25)

    def test_unknown_duplicate_missing_and_invalid_values_are_rejected(self):
        original = self.source.read_text(encoding="utf-8")
        variants = (
            original + "\nTYPO=1\n",
            original + "\nWORKERS=2\n",
            original.replace("WORKERS=50\n", ""),
            original.replace("WORKERS=50", "WORKERS=lots"),
        )
        for text in variants:
            with self.subTest(text=text[-30:]), self.assertRaises(ConfigError):
                self._load_text(text)


if __name__ == "__main__":
    unittest.main()
