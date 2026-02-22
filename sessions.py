"""
sessions.py — Manage multiple Facebook account sessions.

Each account gets its own persistent browser profile directory.
Pages in config.json can be mapped to specific accounts.
A special "anonymous" account uses no login (for fully public pages).

First-time setup for each account:
    python fb_monitor.py --login <account_name>

This opens a visible browser window where you log in manually.
The session is saved and reused on subsequent runs.
"""

import logging
import shutil
from pathlib import Path

from playwright.sync_api import sync_playwright, BrowserContext

from stealth import create_stealth_context, get_tor_proxy

log = logging.getLogger("fb-monitor")

PROFILES_DIR = Path(__file__).parent / "profiles"


def get_profile_dir(account_name: str) -> Path:
    """Get the persistent profile directory for an account."""
    profile_dir = PROFILES_DIR / account_name
    profile_dir.mkdir(parents=True, exist_ok=True)
    return profile_dir


def list_accounts() -> list[str]:
    """List all accounts that have saved profiles."""
    if not PROFILES_DIR.exists():
        return []
    return [
        d.name for d in PROFILES_DIR.iterdir()
        if d.is_dir() and d.name != "anonymous"
    ]


def delete_account(account_name: str) -> bool:
    """Delete a saved account profile."""
    profile_dir = PROFILES_DIR / account_name
    if profile_dir.exists():
        shutil.rmtree(profile_dir)
        log.info(f"Deleted profile for '{account_name}'")
        return True
    return False


def interactive_login(account_name: str):
    """
    Open a visible browser for manual Facebook login.
    The session is saved to the account's profile directory.
    """
    profile_dir = get_profile_dir(account_name)

    print(f"\n{'=' * 50}")
    print(f"Login session for account: {account_name}")
    print(f"Profile directory: {profile_dir}")
    print(f"{'=' * 50}")
    print()
    print("A browser window will open to facebook.com.")
    print("Please log in with the account you want to use.")
    print("When you're done and see the Facebook homepage,")
    print("close the browser window or press Ctrl+C here.")
    print()

    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )

        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded")

        print("\n⏳ Waiting for you to log in... (close browser when done)")

        try:
            # Wait until the browser is closed by the user
            page.wait_for_event("close", timeout=0)
        except Exception:
            pass

        try:
            browser.close()
        except Exception:
            pass

    print(f"\n✅ Session saved for '{account_name}'")
    print(f"   Profile: {profile_dir}")


def create_session_context(
    browser_or_pw,
    account_name: str,
    config: dict,
    use_stealth: bool = True,
) -> tuple:
    """
    Create a browser context for the given account.

    For named accounts: uses persistent profile (keeps cookies/session).
    For "anonymous": uses a fresh stealth context (no login).

    Returns:
        (context, needs_close) — needs_close is True if the caller
        should close the context when done. Persistent contexts
        are the browser itself, so closing works differently.
    """
    if account_name == "anonymous" or not account_name:
        # Fresh context, no saved session — route through Tor if enabled
        proxy = get_tor_proxy(config)
        launch_kwargs = {"headless": config.get("headless", True)}
        if proxy:
            launch_kwargs["proxy"] = proxy
        browser = browser_or_pw.chromium.launch(**launch_kwargs)
        context = create_stealth_context(browser, config)
        return context, browser, True

    # Persistent context with saved cookies — never use Tor
    # (logged-in accounts go direct to avoid triggering verification)
    profile_dir = get_profile_dir(account_name)

    if not any(profile_dir.iterdir()):
        log.warning(
            f"No saved session for '{account_name}'. "
            f"Run: python fb_monitor.py --login {account_name}"
        )

    from stealth import random_user_agent, random_viewport, USER_AGENTS
    import random

    ua = random_user_agent()
    viewport = random_viewport()

    context = browser_or_pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=config.get("headless", True),
        viewport=viewport,
        user_agent=ua,
        locale="en-US",
    )

    # Add stealth scripts
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) =>
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters);
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5],
        });
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
        });
    """)

    return context, None, False


def get_account_for_page(page_config: dict, config: dict) -> str:
    """
    Determine which account to use for a given page.

    Priority:
    1. page-level "account" field
    2. global "default_account" in config
    3. "anonymous" (no login)
    """
    # Page-level override
    account = page_config.get("account", "")
    if account:
        return account

    # Global default
    account = config.get("default_account", "")
    if account:
        return account

    return "anonymous"


def group_pages_by_account(config: dict) -> dict[str, list[dict]]:
    """
    Group enabled pages by their account.
    Returns {account_name: [page_configs]}.
    """
    groups: dict[str, list[dict]] = {}

    for page_cfg in config.get("pages", []):
        if not page_cfg.get("enabled", True):
            continue
        account = get_account_for_page(page_cfg, config)
        groups.setdefault(account, []).append(page_cfg)

    return groups
