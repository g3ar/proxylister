"""Optional Chrome/Selenium validation after lightweight URL checks pass."""

import json
import time

from proxylister.checking.proxy import connection_string
from proxylister.models import ProxyResult

CHECK_URL_HOLD_SECONDS = 10
MIN_PAGE_LOAD_TIMEOUT = 10


def _final_document_status(driver):
    try:
        statuses = []
        for entry in driver.get_log("performance"):
            message = json.loads(entry["message"]).get("message", {})
            params = message.get("params", {})
            if message.get("method") == "Network.responseReceived" and params.get("type") == "Document":
                statuses.append(params.get("response", {}).get("status"))
        return statuses[-1] if statuses else None
    except Exception:
        return None


def verify_in_browser(result: ProxyResult, url: str, page_load_timeout: float, headless: bool) -> bool:
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException, WebDriverException
    except ImportError as exc:
        raise RuntimeError("Selenium is required for --browser-check; run through ./proxylister") from exc

    options = webdriver.ChromeOptions()
    options.add_argument(f"--proxy-server={connection_string(result.protocol, result.proxy)}")
    if headless:
        options.add_argument("--headless=new")
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    driver = None
    try:
        driver = webdriver.Chrome(options=options)
        driver.set_page_load_timeout(page_load_timeout)
        driver.get(url)
        document_uri = driver.execute_script("return document.documentURI")
        final_status = _final_document_status(driver)
        if document_uri.startswith("chrome-error://") or (final_status is not None and final_status >= 400):
            return False
        if not headless:
            time.sleep(CHECK_URL_HOLD_SECONDS)
        return True
    except (TimeoutException, WebDriverException):
        return False
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


def browser_check(
    result: ProxyResult,
    url: str,
    page_load_timeout: float,
    headless: bool,
) -> ProxyResult | None:
    return result if verify_in_browser(result, url, page_load_timeout, headless) else None
