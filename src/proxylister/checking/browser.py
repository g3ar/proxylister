"""Optional multi-browser Selenium validation after lightweight checks pass."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
import time

from proxylister.checking.proxy import connection_string
from proxylister.external_process import external_program_environment
from proxylister.models import ProxyResult

CHECK_URL_HOLD_SECONDS = 10
MIN_PAGE_LOAD_TIMEOUT = 10
SELENIUM_MANAGER_TIMEOUT = 30


def _selenium():
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException, WebDriverException
        from selenium.webdriver.common.proxy import Proxy
    except ImportError as exc:
        raise RuntimeError(
            "Selenium is required for browser checks; run through ./proxylister"
        ) from exc
    return webdriver, TimeoutException, WebDriverException, Proxy


def _firefox_proxy(options, result: ProxyResult) -> None:
    host, port_text = result.proxy.rsplit(":", 1)
    port = int(port_text)
    options.set_preference("network.proxy.type", 1)
    if result.protocol == "http":
        options.set_preference("network.proxy.http", host)
        options.set_preference("network.proxy.http_port", port)
        options.set_preference("network.proxy.ssl", host)
        options.set_preference("network.proxy.ssl_port", port)
    else:
        options.set_preference("network.proxy.socks", host)
        options.set_preference("network.proxy.socks_port", port)
        options.set_preference(
            "network.proxy.socks_version", 4 if result.protocol == "socks4" else 5
        )


def _standard_proxy(proxy_class, result: ProxyResult):
    proxy = proxy_class()
    if result.protocol == "http":
        proxy.http_proxy = result.proxy
        proxy.ssl_proxy = result.proxy
    else:
        proxy.socks_proxy = result.proxy
        proxy.socks_version = 4 if result.protocol == "socks4" else 5
    return proxy


def _options(family: str, result: ProxyResult | None, headless: bool):
    webdriver, _timeout, _webdriver_error, proxy_class = _selenium()
    if family == "chrome":
        options = webdriver.ChromeOptions()
        options.add_argument("--incognito")
        if headless:
            options.add_argument("--headless=new")
        if result is not None:
            options.add_argument(
                f"--proxy-server={connection_string(result.protocol, result.proxy)}"
            )
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        return options
    if family == "edge":
        options = webdriver.EdgeOptions()
        options.add_argument("--inprivate")
        if headless:
            options.add_argument("--headless=new")
        if result is not None:
            options.add_argument(
                f"--proxy-server={connection_string(result.protocol, result.proxy)}"
            )
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        return options
    if family == "firefox":
        options = webdriver.FirefoxOptions()
        options.add_argument("-private")
        if headless:
            options.add_argument("-headless")
        if result is not None:
            _firefox_proxy(options, result)
        return options
    if family == "safari":
        if headless:
            raise RuntimeError("Safari does not support headless WebDriver sessions")
        options = webdriver.SafariOptions()
        if result is not None:
            options.proxy = _standard_proxy(proxy_class, result)
        return options
    raise ValueError(f"unsupported browser family: {family}")


@contextmanager
def _selenium_manager_defaults():
    """Bound manager network waits and avoid downloading whole browsers."""
    defaults = {
        "SE_TIMEOUT": str(SELENIUM_MANAGER_TIMEOUT),
        "SE_AVOID_BROWSER_DOWNLOAD": "true",
    }
    added = []
    for name, value in defaults.items():
        if name not in os.environ:
            os.environ[name] = value
            added.append(name)
    try:
        yield
    finally:
        for name in added:
            os.environ.pop(name, None)


def _start_driver(family: str, result: ProxyResult | None, headless: bool):
    webdriver, _timeout, _webdriver_error, _proxy = _selenium()
    options = _options(family, result, headless)
    constructors = {
        "chrome": webdriver.Chrome,
        "firefox": webdriver.Firefox,
        "edge": webdriver.Edge,
        "safari": webdriver.Safari,
    }
    with _selenium_manager_defaults(), external_program_environment():
        return constructors[family](options=options)


def probe_selenium_browser(family: str, headless: bool) -> bool:
    """Let Selenium Manager prove one browser/driver combination on this host."""
    driver = None
    try:
        driver = _start_driver(family, None, headless)
        driver.set_page_load_timeout(MIN_PAGE_LOAD_TIMEOUT)
        driver.get("about:blank")
        return True
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def _final_document_status(driver):
    try:
        statuses = []
        for entry in driver.get_log("performance"):
            message = json.loads(entry["message"]).get("message", {})
            params = message.get("params", {})
            if (
                message.get("method") == "Network.responseReceived"
                and params.get("type") == "Document"
            ):
                statuses.append(params.get("response", {}).get("status"))
        return statuses[-1] if statuses else None
    except Exception:
        return None


def _validate_page(driver, url: str, page_load_timeout: float, headless: bool) -> bool:
    _webdriver, TimeoutException, WebDriverException, _proxy = _selenium()
    try:
        driver.set_page_load_timeout(page_load_timeout)
        driver.get(url)
        document_uri = driver.execute_script("return document.documentURI")
        final_status = _final_document_status(driver)
        if document_uri.startswith(("chrome-error://", "about:neterror")) or (
            final_status is not None and final_status >= 400
        ):
            return False
        if not headless:
            time.sleep(CHECK_URL_HOLD_SECONDS)
        return True
    except (TimeoutException, WebDriverException):
        return False


class SeleniumBrowserSelector:
    """Reuse the first launchable detected family for sequential browser checks."""

    def __init__(self, candidates: tuple[str, ...]):
        self.candidates = candidates
        self.selected: str | None = None

    def verify(
        self,
        result: ProxyResult,
        url: str,
        page_load_timeout: float,
        headless: bool,
    ) -> bool:
        order = (
            (self.selected,)
            + tuple(item for item in self.candidates if item != self.selected)
            if self.selected is not None
            else self.candidates
        )
        for family in order:
            driver = None
            try:
                driver = _start_driver(family, result, headless)
            except Exception:
                if family == self.selected:
                    self.selected = None
                continue
            try:
                self.selected = family
                return _validate_page(driver, url, page_load_timeout, headless)
            finally:
                try:
                    driver.quit()
                except Exception:
                    pass
        return False


def verify_in_browser(
    result: ProxyResult,
    url: str,
    page_load_timeout: float,
    headless: bool,
    *,
    browsers: tuple[str, ...] = ("chrome",),
) -> bool:
    return SeleniumBrowserSelector(browsers).verify(
        result, url, page_load_timeout, headless
    )


def browser_check(
    result: ProxyResult,
    url: str,
    page_load_timeout: float,
    headless: bool,
    selector: SeleniumBrowserSelector | None = None,
) -> ProxyResult | None:
    selector = selector or SeleniumBrowserSelector(("chrome",))
    return result if selector.verify(result, url, page_load_timeout, headless) else None
