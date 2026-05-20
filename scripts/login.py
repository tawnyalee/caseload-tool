"""Open a browser using the persistent context so you can log into Caseload
once. The session (SSO cookies, Salesforce auth) is saved into
./browser_data/ and reused by every subsequent automation run.

Usage:
    python -m scripts.login
"""
import sys
from pathlib import Path

# Allow running as a plain script (python scripts/login.py) too.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.browser import persistent_context
from src.config import CASELOAD_URL


def main() -> None:
    with persistent_context() as context:
        page = context.pages[0] if context.pages else context.new_page()
        if CASELOAD_URL:
            page.goto(CASELOAD_URL)
        else:
            print("CASELOAD_URL not set in .env — opening blank page.")
        print()
        print("Browser is open. Complete SSO / Salesforce login in the window.")
        print("When you're logged in, press Enter HERE to close the browser.")
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass


if __name__ == "__main__":
    main()
