from contextlib import contextmanager
from typing import Iterator

from playwright.sync_api import BrowserContext, sync_playwright

from src.config import BROWSER_DATA_DIR


@contextmanager
def persistent_context(headless: bool = False) -> Iterator[BrowserContext]:
    """Launch Chromium with a persistent user-data dir so SSO/Salesforce
    cookies survive across runs. Log in once via scripts/login.py and every
    subsequent automation run reuses the session."""
    BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_DATA_DIR),
            headless=headless,
            viewport={"width": 1400, "height": 900},
        )
        try:
            yield context
        finally:
            context.close()
