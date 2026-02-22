"""
stealth.py — Anti-detection measures for Facebook monitoring.

Provides:
- Randomized check intervals with jitter
- Randomized delays between page loads
- Human-like scroll behavior
- Rotating user agents
- Request rate tracking to stay under thresholds
"""

import logging
import math
import random
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("fb-monitor")


# ---------------------------------------------------------------------------
# User agent rotation
# ---------------------------------------------------------------------------

USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome on Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    # Firefox on Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) Gecko/20100101 Firefox/122.0",
    # Edge
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    # Safari
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

# Viewport sizes that match real browser windows
VIEWPORT_SIZES = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
    {"width": 1280, "height": 720},
]


def random_user_agent() -> str:
    return random.choice(USER_AGENTS)


def random_viewport() -> dict:
    return random.choice(VIEWPORT_SIZES)


# ---------------------------------------------------------------------------
# Timing / jitter
# ---------------------------------------------------------------------------

def jittered_interval(base_minutes: int, jitter_pct: float = 0.4) -> float:
    """
    Return a randomized interval in seconds.

    Given a base of 15 minutes and 40% jitter:
    - Minimum: 15 * 0.6 = 9 minutes
    - Maximum: 15 * 1.4 = 21 minutes

    The distribution is gaussian-ish (triangular) so most values
    cluster near the base, with occasional longer/shorter gaps.
    """
    low = base_minutes * (1 - jitter_pct)
    high = base_minutes * (1 + jitter_pct)

    # Triangular distribution — peaks at the base value
    interval = random.triangular(low, high, base_minutes)
    return interval * 60  # convert to seconds


def human_delay(min_sec: float = 1.0, max_sec: float = 4.0) -> float:
    """
    Random delay simulating human page-viewing time.
    Uses log-normal distribution — mostly short pauses,
    occasionally longer ones.
    """
    mu = math.log((min_sec + max_sec) / 2)
    sigma = 0.5
    delay = random.lognormvariate(mu, sigma)
    return max(min_sec, min(delay, max_sec * 2))


def human_scroll_delay() -> float:
    """Delay between scroll actions (faster than page loads)."""
    return random.uniform(0.8, 2.5)


# ---------------------------------------------------------------------------
# Human-like scrolling
# ---------------------------------------------------------------------------

def human_scroll(page, scroll_count: int = 3):
    """
    Scroll down the page with human-like timing and variation.
    Sometimes scrolls a little, sometimes a lot. Occasionally pauses.
    """
    for i in range(scroll_count):
        # Vary scroll distance
        distance = random.randint(300, 900)
        page.evaluate(f"window.scrollBy(0, {distance})")

        delay = human_scroll_delay()

        # Occasionally pause longer (as if reading)
        if random.random() < 0.2:
            delay += random.uniform(2, 5)

        # Occasionally scroll up slightly (natural behavior)
        if random.random() < 0.1 and i > 0:
            up = random.randint(50, 200)
            page.evaluate(f"window.scrollBy(0, -{up})")
            time.sleep(random.uniform(0.3, 0.8))

        page.wait_for_timeout(int(delay * 1000))


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """
    Track request rate and enforce limits.

    Ensures we don't exceed a configurable number of page loads
    per hour. If we're approaching the limit, adds delays.
    """

    def __init__(self, max_per_hour: int = 30):
        self.max_per_hour = max_per_hour
        self.requests: list[float] = []  # timestamps

    def _prune(self):
        """Remove entries older than 1 hour."""
        cutoff = time.time() - 3600
        self.requests = [t for t in self.requests if t > cutoff]

    def record(self):
        """Record a request."""
        self.requests.append(time.time())

    def count_last_hour(self) -> int:
        """How many requests in the last hour."""
        self._prune()
        return len(self.requests)

    def should_wait(self) -> Optional[float]:
        """
        If we're near the rate limit, return seconds to wait.
        Returns None if we're fine to proceed.
        """
        self._prune()
        count = len(self.requests)

        if count >= self.max_per_hour:
            # Wait until the oldest request falls out of the window
            oldest = min(self.requests)
            wait = (oldest + 3600) - time.time() + random.uniform(10, 60)
            return max(0, wait)

        # If we're above 80% of the limit, add a small delay
        if count > self.max_per_hour * 0.8:
            return random.uniform(30, 90)

        return None

    def wait_if_needed(self):
        """Block if we're near the rate limit."""
        wait = self.should_wait()
        if wait and wait > 0:
            log.info(f"  ⏳ Rate limit: waiting {wait:.0f}s ({self.count_last_hour()}/{self.max_per_hour} requests/hr)")
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Browser context factory
# ---------------------------------------------------------------------------

def get_tor_proxy(config: dict) -> dict | None:
    """Return Playwright proxy dict if Tor is enabled, else None."""
    tor_cfg = config.get("tor", {})
    if not tor_cfg.get("enabled", False):
        return None
    port = tor_cfg.get("socks_port", 9050)
    return {"server": f"socks5://127.0.0.1:{port}"}


def create_stealth_context(browser, config: dict):
    """
    Create a browser context with randomized fingerprint.
    Each cycle gets a fresh context with different characteristics.
    """
    ua = random_user_agent()
    viewport = random_viewport()

    # Randomize locale slightly
    locales = ["en-US", "en-US", "en-US", "en-GB", "en-CA"]  # weighted toward en-US

    ctx_kwargs = dict(
        user_agent=ua,
        viewport=viewport,
        locale=random.choice(locales),
        timezone_id=random.choice([
            "America/Los_Angeles", "America/Denver",
            "America/Chicago", "America/New_York",
        ]),
    )

    proxy = get_tor_proxy(config)
    if proxy:
        ctx_kwargs["proxy"] = proxy

    context = browser.new_context(**ctx_kwargs)

    # Add some stealth JavaScript to mask automation
    context.add_init_script("""
        // Override navigator.webdriver
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

        // Override chrome.runtime to look like a real browser
        window.chrome = { runtime: {} };

        // Override permissions query
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) =>
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters);

        // Override plugins to look non-empty
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5],
        });

        // Override languages
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
        });
    """)

    log.debug(f"Browser context: {ua[:50]}... viewport={viewport['width']}x{viewport['height']}")

    return context


# ---------------------------------------------------------------------------
# Page load with delays
# ---------------------------------------------------------------------------

def stealth_goto(page, url: str, timeout: int = 30000):
    """
    Navigate to a URL with a human-like pre-delay.
    """
    # Small random delay before navigation
    pre_delay = human_delay(0.5, 2.0)
    time.sleep(pre_delay)

    page.goto(url, wait_until="domcontentloaded", timeout=timeout)

    # Random post-load delay (as if the page is rendering and user is looking)
    post_delay = human_delay(2.0, 5.0)
    page.wait_for_timeout(int(post_delay * 1000))
