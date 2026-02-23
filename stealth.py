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
# User agent / fingerprint rotation
# ---------------------------------------------------------------------------

# Each entry is a coherent identity: UA string, platform, vendor, and browser type.
# Platform must match the UA — a Mac UA with Win32 platform is a dead giveaway.
BROWSER_PROFILES = [
    # Chrome on Windows
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
     "platform": "Win32", "vendor": "Google Inc.", "browser": "chrome"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
     "platform": "Win32", "vendor": "Google Inc.", "browser": "chrome"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
     "platform": "Win32", "vendor": "Google Inc.", "browser": "chrome"},
    # Chrome on Mac
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
     "platform": "MacIntel", "vendor": "Google Inc.", "browser": "chrome"},
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
     "platform": "MacIntel", "vendor": "Google Inc.", "browser": "chrome"},
    # Firefox on Windows
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
     "platform": "Win32", "vendor": "", "browser": "firefox"},
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
     "platform": "Win32", "vendor": "", "browser": "firefox"},
    # Firefox on Mac
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) Gecko/20100101 Firefox/122.0",
     "platform": "MacIntel", "vendor": "", "browser": "firefox"},
    # Edge on Windows
    {"ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
     "platform": "Win32", "vendor": "Google Inc.", "browser": "chrome"},
    # Safari on Mac
    {"ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
     "platform": "MacIntel", "vendor": "Apple Computer, Inc.", "browser": "safari"},
]

# Backward compat — flat list of UA strings used by sessions.py
USER_AGENTS = [p["ua"] for p in BROWSER_PROFILES]

# Viewport sizes that match real browser windows
VIEWPORT_SIZES = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
    {"width": 1280, "height": 800},
    {"width": 1280, "height": 720},
]

# Common WebGL renderer strings to spoof (avoids leaking real GPU)
WEBGL_RENDERERS = [
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630, OpenGL 4.5)"},
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, OpenGL 4.5)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 6GB, OpenGL 4.5)"},
    {"vendor": "Google Inc. (NVIDIA)", "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060, OpenGL 4.5)"},
    {"vendor": "Google Inc. (AMD)", "renderer": "ANGLE (AMD, AMD Radeon RX 580, OpenGL 4.5)"},
    {"vendor": "Google Inc. (AMD)", "renderer": "ANGLE (AMD, AMD Radeon(TM) Graphics, OpenGL 4.5)"},
    {"vendor": "Google Inc. (Intel)", "renderer": "ANGLE (Intel, Intel(R) HD Graphics 620, OpenGL 4.5)"},
]


def random_browser_profile() -> dict:
    """Return a coherent browser identity (UA + platform + vendor + browser type)."""
    return random.choice(BROWSER_PROFILES)


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

    def reset(self):
        """Clear all recorded requests (used after Tor circuit renewal)."""
        self.requests.clear()

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

    def wait_if_needed(self, rotation_callback=None):
        """
        Block if we're near the rate limit.

        If a rotation_callback is provided and the wait would be long (>60s),
        calls the callback instead of waiting. The callback should rotate the
        Tor circuit and return True on success, after which the counter is reset.
        """
        wait = self.should_wait()
        if wait and wait > 0:
            # If we'd wait a long time and can rotate, do that instead
            if rotation_callback and wait > 60:
                log.info(f"  Rate limit hit ({self.count_last_hour()}/{self.max_per_hour}/hr) — requesting Tor rotation...")
                if rotation_callback():
                    self.reset()
                    return
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


def get_tor_proxy_for_port(socks_port: int) -> dict:
    """Return Playwright proxy dict for a specific SOCKS port."""
    return {"server": f"socks5://127.0.0.1:{socks_port}"}


def renew_tor_circuit(config: dict) -> bool:
    """
    Request a new Tor circuit via the control port (SIGNAL NEWNYM).

    This gives us a new exit node / IP address. Requires Tor's ControlPort
    to be enabled (typically port 9051). Returns True on success.

    To enable in torrc:
        ControlPort 9051
        CookieAuthentication 1
    Or with a password:
        HashedControlPassword <hash>
    """
    import socket

    tor_cfg = config.get("tor", {})
    if not tor_cfg.get("enabled"):
        return False

    control_port = tor_cfg.get("control_port", 9051)
    password = tor_cfg.get("control_password", "")

    try:
        sock = socket.create_connection(("127.0.0.1", control_port), timeout=10)

        # Authenticate
        if password:
            sock.sendall(f'AUTHENTICATE "{password}"\r\n'.encode())
        else:
            sock.sendall(b"AUTHENTICATE\r\n")
        response = sock.recv(256).decode()
        if "250" not in response:
            log.warning(f"Tor control auth failed: {response.strip()}")
            sock.close()
            return False

        # Request new circuit
        sock.sendall(b"SIGNAL NEWNYM\r\n")
        response = sock.recv(256).decode()
        sock.close()

        if "250" in response:
            log.info("Tor circuit renewed — new exit node")
            # Tor needs a moment to build the new circuit
            time.sleep(random.uniform(3, 6))
            return True
        else:
            log.warning(f"Tor NEWNYM failed: {response.strip()}")
            return False

    except Exception as e:
        log.warning(f"Tor circuit renewal failed: {e}")
        return False


_SENTINEL = object()  # distinguishes "not provided" from None


def create_stealth_context(browser, config: dict, proxy_override=_SENTINEL):
    """
    Create a browser context with a fully coherent randomized fingerprint.

    Each session gets a consistent identity: UA, platform, vendor, viewport,
    screen size, WebGL renderer, canvas noise, timezone, and locale all match
    so nothing looks contradictory to fingerprinting scripts.

    proxy_override: if provided, uses this proxy dict instead of reading from
    config. Pass None to explicitly disable proxy, or a dict like
    {"server": "socks5://127.0.0.1:9060"} to use a specific port.
    Omit (default sentinel) to read from config as before.
    """
    profile = random_browser_profile()
    viewport = random_viewport()
    webgl = random.choice(WEBGL_RENDERERS)

    # Screen size should be >= viewport (as if browser isn't maximized)
    screen_w = viewport["width"] + random.choice([0, 0, 80, 160])
    screen_h = viewport["height"] + random.choice([0, 0, 40, 80])

    # Randomize locale slightly
    locales = ["en-US", "en-US", "en-US", "en-GB", "en-CA"]  # weighted toward en-US

    ctx_kwargs = dict(
        user_agent=profile["ua"],
        viewport=viewport,
        locale=random.choice(locales),
        timezone_id=random.choice([
            "America/Los_Angeles", "America/Denver",
            "America/Chicago", "America/New_York",
        ]),
        # Device scale factor — varies by display
        device_scale_factor=random.choice([1, 1, 1, 1.25, 1.5, 2]),
        color_scheme=random.choice(["light", "light", "light", "dark"]),
    )

    if proxy_override is not _SENTINEL:
        proxy = proxy_override
    else:
        proxy = get_tor_proxy(config)
    if proxy:
        ctx_kwargs["proxy"] = proxy

    context = browser.new_context(**ctx_kwargs)

    # Build stealth script tailored to this session's identity
    is_chrome = profile["browser"] in ("chrome", "safari")
    platform = profile["platform"]
    vendor = profile["vendor"]

    context.add_init_script(f"""
        // --- Core: hide automation ---
        Object.defineProperty(navigator, 'webdriver', {{ get: () => undefined }});

        // --- Platform: must match UA string ---
        Object.defineProperty(navigator, 'platform', {{ get: () => '{platform}' }});
        Object.defineProperty(navigator, 'vendor', {{ get: () => '{vendor}' }});

        // --- Chrome object: only present in Chrome/Edge/Safari ---
        {'window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };' if is_chrome else 'delete window.chrome;'}

        // --- Permissions ---
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) =>
            parameters.name === 'notifications'
                ? Promise.resolve({{ state: Notification.permission }})
                : originalQuery(parameters);

        // --- Plugins: Chrome has plugins, Firefox doesn't ---
        Object.defineProperty(navigator, 'plugins', {{
            get: () => {'[1, 2, 3, 4, 5]' if is_chrome else '[]'},
        }});

        // --- Languages ---
        Object.defineProperty(navigator, 'languages', {{
            get: () => ['en-US', 'en'],
        }});

        // --- Hardware concurrency: randomize CPU core count ---
        Object.defineProperty(navigator, 'hardwareConcurrency', {{
            get: () => {random.choice([4, 8, 8, 12, 16])},
        }});

        // --- Device memory (Chrome only, in GB) ---
        Object.defineProperty(navigator, 'deviceMemory', {{
            get: () => {random.choice([4, 8, 8, 16])},
        }});

        // --- Screen dimensions: must be >= viewport ---
        Object.defineProperty(screen, 'width', {{ get: () => {screen_w} }});
        Object.defineProperty(screen, 'height', {{ get: () => {screen_h} }});
        Object.defineProperty(screen, 'availWidth', {{ get: () => {screen_w} }});
        Object.defineProperty(screen, 'availHeight', {{ get: () => {screen_h - random.randint(30, 50)} }});
        Object.defineProperty(screen, 'colorDepth', {{ get: () => 24 }});
        Object.defineProperty(screen, 'pixelDepth', {{ get: () => 24 }});

        // --- WebGL fingerprint: spoof renderer to avoid leaking real GPU ---
        const getParameterOrig = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(param) {{
            const UNMASKED_VENDOR = 0x9245;
            const UNMASKED_RENDERER = 0x9246;
            if (param === UNMASKED_VENDOR) return '{webgl["vendor"]}';
            if (param === UNMASKED_RENDERER) return '{webgl["renderer"]}';
            return getParameterOrig.call(this, param);
        }};
        if (typeof WebGL2RenderingContext !== 'undefined') {{
            const getParam2Orig = WebGL2RenderingContext.prototype.getParameter;
            WebGL2RenderingContext.prototype.getParameter = function(param) {{
                const UNMASKED_VENDOR = 0x9245;
                const UNMASKED_RENDERER = 0x9246;
                if (param === UNMASKED_VENDOR) return '{webgl["vendor"]}';
                if (param === UNMASKED_RENDERER) return '{webgl["renderer"]}';
                return getParam2Orig.call(this, param);
            }};
        }}

        // --- Canvas fingerprint: add subtle noise so hash differs per session ---
        const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
        const _toBlob = HTMLCanvasElement.prototype.toBlob;
        const _getImageData = CanvasRenderingContext2D.prototype.getImageData;

        // Per-session noise seed (consistent within one page load)
        const _noiseSeed = {random.randint(1, 2**31)};
        function _noiseHash(x) {{
            x = ((x >> 16) ^ x) * 0x45d9f3b;
            x = ((x >> 16) ^ x) * 0x45d9f3b;
            x = (x >> 16) ^ x;
            return x;
        }}

        CanvasRenderingContext2D.prototype.getImageData = function(sx, sy, sw, sh) {{
            const imageData = _getImageData.call(this, sx, sy, sw, sh);
            // Only add noise to small reads (fingerprinting), not large captures
            if (sw * sh < 500 * 500) {{
                for (let i = 0; i < imageData.data.length; i += 4) {{
                    const noise = (_noiseHash(_noiseSeed + i) % 3) - 1;  // -1, 0, or 1
                    imageData.data[i] = Math.max(0, Math.min(255, imageData.data[i] + noise));
                }}
            }}
            return imageData;
        }};
    """)

    log.debug(f"Stealth context: {profile['ua'][:40]}... "
              f"platform={platform} viewport={viewport['width']}x{viewport['height']} "
              f"gpu={webgl['renderer'][:30]}...")

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
