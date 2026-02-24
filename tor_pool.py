"""
tor_pool.py — Manage N parallel Tor processes for racing connections.

Facebook blocks ~80-90% of Tor exit IPs. By running multiple Tor instances
on separate ports and racing them in parallel, we can find a working exit
node much faster than sequential retries on a single instance.

Each instance gets its own SocksPort, ControlPort, DataDirectory, and
generated torrc file. A background health monitor thread checks bootstrap
progress, detects stalls and crashes, and auto-restarts unhealthy instances.

Usage:
    pool = TorPool(config)
    pool.start()
    ready = pool.wait_ready(timeout=120)
    healthy = pool.get_healthy()
    raceable = pool.get_raceable(cooldown=300)
    pool.renew_circuit(instance)
    pool.record_probe_result(instance, success, duration)
    pool.record_login_wall(instance)
    pool.stop()
"""

import atexit
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

log = logging.getLogger("fb-monitor")

BASE_DIR = Path(__file__).parent
TOR_BUNDLE_DIR = BASE_DIR / "tor-bundle"
TORRC_TEMPLATE = TOR_BUNDLE_DIR / "torrc"
POOL_DATA_DIR = TOR_BUNDLE_DIR / "tor-data-pool"
PID_FILE = POOL_DATA_DIR / "pool-pids.json"
MAIN_PID_FILE = TOR_BUNDLE_DIR / "main-tor-pid.json"
TOR_EXE = TOR_BUNDLE_DIR / "tor" / "tor.exe"


# ------------------------------------------------------------------
# Standalone: kill all stale Tor processes from our bundle
# ------------------------------------------------------------------

def _kill_pid(pid: int, label: str = "") -> bool:
    """Kill a single process by PID. Returns True if killed."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        else:
            os.kill(pid, signal.SIGKILL)
        log.debug(f"  Killed stale Tor PID {pid}{f' ({label})' if label else ''}")
        return True
    except Exception:
        return False


def _is_port_in_use(port: int) -> bool:
    """Check if a TCP port is already bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _get_pid_on_port(port: int) -> int | None:
    """Find the PID listening on a port (Windows only)."""
    if os.name != "nt":
        return None
    try:
        result = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=10,
        )
        for line in result.stdout.splitlines():
            if f"127.0.0.1:{port}" in line and "LISTENING" in line:
                parts = line.split()
                return int(parts[-1])
    except Exception:
        pass
    return None


def kill_all_stale_tor(config: dict) -> int:
    """
    Kill ALL stale Tor processes from previous runs.

    Covers:
    1. Saved PIDs from pool-pids.json and main-tor-pid.json
    2. Anything listening on main Tor ports (9050/9051)
    3. Anything listening on pool ports (9060+ / 9160+)

    Call this at scraper startup before starting anything Tor-related.
    Returns number of processes killed.
    """
    killed = 0
    tor_cfg = config.get("tor", {})
    main_socks = tor_cfg.get("socks_port", 9050)
    main_control = tor_cfg.get("control_port", 9051)
    pool_size = tor_cfg.get("pool_size", 0)
    pool_base = tor_cfg.get("pool_base_socks_port", 9060)

    # 1. Kill from saved PID files
    for pid_file in (MAIN_PID_FILE, PID_FILE):
        if pid_file.exists():
            try:
                saved = json.loads(pid_file.read_text())
                entries = saved if isinstance(saved, list) else [saved]
                for entry in entries:
                    pid = entry.get("pid", 0)
                    if pid and _kill_pid(pid, f"saved PID from {pid_file.name}"):
                        killed += 1
                pid_file.unlink(missing_ok=True)
            except Exception as e:
                log.debug(f"  PID file cleanup error ({pid_file.name}): {e}")

    # 2. Kill anything on main Tor ports
    for port in (main_socks, main_control):
        if _is_port_in_use(port):
            pid = _get_pid_on_port(port)
            if pid:
                if _kill_pid(pid, f"main Tor port {port}"):
                    killed += 1

    # 3. Kill anything on pool ports
    if pool_size >= 2:
        for i in range(pool_size):
            for port in (pool_base + i, pool_base + 100 + i):
                if _is_port_in_use(port):
                    pid = _get_pid_on_port(port)
                    if pid:
                        if _kill_pid(pid, f"pool port {port}"):
                            killed += 1

    if killed:
        time.sleep(2)  # Windows needs time to release handles
        log.info(f"  Cleaned up {killed} stale Tor process(es) from previous run")

    return killed


def ensure_main_tor(config: dict) -> subprocess.Popen | None:
    """
    Start the main Tor instance (port 9050) if not already running.

    Returns the Popen handle if we started it, None if already running
    or Tor is disabled.
    """
    tor_cfg = config.get("tor", {})
    if not tor_cfg.get("enabled", False):
        return None

    socks_port = tor_cfg.get("socks_port", 9050)

    # Already running and responsive?
    if _is_port_in_use(socks_port):
        log.info(f"Main Tor already running on port {socks_port}")
        return None

    if not TOR_EXE.exists():
        log.error(f"tor.exe not found: {TOR_EXE}")
        return None

    if not TORRC_TEMPLATE.exists():
        log.error(f"torrc not found: {TORRC_TEMPLATE}")
        return None

    log.info(f"Starting main Tor instance (SOCKS:{socks_port})...")

    # Build absolute-path torrc for main instance
    # The base torrc uses relative paths — we need absolute for subprocess
    base_lines = TORRC_TEMPLATE.read_text().splitlines()
    resolved_lines = []
    for line in base_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            resolved_lines.append(line)
            continue
        key = stripped.split()[0].lower() if stripped.split() else ""
        if key == "clienttransportplugin":
            parts = stripped.split()
            for i, part in enumerate(parts):
                if part.startswith("./"):
                    parts[i] = str(TOR_BUNDLE_DIR / part[2:])
            resolved_lines.append(" ".join(parts))
        elif key in ("geoipfile", "geoipv6file"):
            parts = stripped.split(maxsplit=1)
            if len(parts) == 2 and parts[1].startswith("./"):
                parts[1] = str(TOR_BUNDLE_DIR / parts[1][2:])
            resolved_lines.append(" ".join(parts))
        elif key == "datadirectory":
            parts = stripped.split(maxsplit=1)
            if len(parts) == 2 and parts[1].startswith("./"):
                parts[1] = str(TOR_BUNDLE_DIR / parts[1][2:])
            resolved_lines.append(" ".join(parts))
        elif key == "log":
            # Redirect stdout to file for managed instance
            log_path = str(TOR_BUNDLE_DIR / "tor_output.log").replace("\\", "/")
            resolved_lines.append(f"Log notice file {log_path}")
        else:
            resolved_lines.append(line)

    # Write resolved torrc
    resolved_torrc = TOR_BUNDLE_DIR / "torrc-main-managed"
    resolved_torrc.write_text("\n".join(resolved_lines))

    # Ensure data dir exists
    data_dir = TOR_BUNDLE_DIR / "tor-data"
    data_dir.mkdir(parents=True, exist_ok=True)

    # Clear stale lock
    lock_file = data_dir / "lock"
    if lock_file.exists():
        try:
            lock_file.unlink()
        except Exception:
            pass

    try:
        proc = subprocess.Popen(
            [str(TOR_EXE), "-f", str(resolved_torrc)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        log.info(f"  Main Tor started: PID {proc.pid}")

        # Save PID for cleanup on next run
        try:
            MAIN_PID_FILE.write_text(json.dumps({
                "pid": proc.pid,
                "socks_port": socks_port,
                "control_port": tor_cfg.get("control_port", 9051),
            }))
        except Exception:
            pass

        # Wait for bootstrap (poll control port)
        deadline = time.time() + 120
        last_pct = 0
        while time.time() < deadline:
            try:
                sock = socket.create_connection(
                    ("127.0.0.1", tor_cfg.get("control_port", 9051)), timeout=5
                )
                pwd = tor_cfg.get("control_password", "")
                if pwd:
                    sock.sendall(f'AUTHENTICATE "{pwd}"\r\n'.encode())
                else:
                    sock.sendall(b"AUTHENTICATE\r\n")
                auth_resp = sock.recv(256).decode()
                if "250" in auth_resp:
                    sock.sendall(b"GETINFO status/bootstrap-phase\r\n")
                    status = sock.recv(512).decode()
                    sock.close()
                    if "PROGRESS=" in status:
                        pct = int(status.split("PROGRESS=")[1].split()[0])
                        if pct != last_pct:
                            log.info(f"  Main Tor bootstrap: {pct}%")
                            last_pct = pct
                        if pct >= 100:
                            log.info("  Main Tor bootstrapped successfully")
                            return proc
                else:
                    sock.close()
            except (ConnectionRefusedError, OSError):
                pass
            except Exception:
                pass
            time.sleep(3)

        log.warning("  Main Tor: bootstrap timeout after 120s")
        return proc  # Return anyway — it may finish later

    except Exception as e:
        log.error(f"  Failed to start main Tor: {e}")
        return None


def stop_main_tor(proc: subprocess.Popen | None):
    """Gracefully stop the main Tor instance we started."""
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    log.info("Main Tor stopped")
    try:
        MAIN_PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


class InstanceState(Enum):
    STARTING = "starting"
    BOOTSTRAPPING = "bootstrapping"
    READY = "ready"
    STALLED = "stalled"        # alive but not making progress
    FAILED = "failed"


@dataclass
class TorInstance:
    index: int
    socks_port: int
    control_port: int
    data_dir: Path
    torrc_path: Path
    log_path: Path
    state: InstanceState = InstanceState.STARTING
    process: subprocess.Popen = field(default=None, repr=False)
    bootstrap_pct: int = 0
    last_error: str = ""
    last_progress_at: float = field(default_factory=time.time)
    restart_count: int = 0
    probe_successes: int = 0
    probe_failures: int = 0
    last_probe_time: float = 0.0       # duration of last successful probe (seconds)
    last_login_wall_at: float = 0.0    # epoch when last login-walled
    _last_control_ok: float = field(default_factory=time.time)


class TorPool:
    """Manages N parallel Tor processes for connection racing."""

    def __init__(self, config: dict):
        tor_cfg = config.get("tor", {})
        self.pool_size = tor_cfg.get("pool_size", 3)
        self.base_socks_port = tor_cfg.get("pool_base_socks_port", 9060)
        self.bootstrap_timeout = tor_cfg.get("pool_bootstrap_timeout", 120)
        self.control_password = tor_cfg.get("control_password", "")
        self.stall_timeout = tor_cfg.get("pool_stall_timeout", 90)
        self.max_restarts = tor_cfg.get("pool_max_restarts", 3)
        self.min_healthy = tor_cfg.get("pool_min_healthy", 2)
        self.control_timeout = 30  # seconds before READY instance marked STALLED
        self.instances: list[TorInstance] = []
        self._monitor_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._last_summary_log: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle: cleanup stale processes, PID tracking, atexit
    # Uses standalone _kill_pid / _is_port_in_use / _get_pid_on_port
    # ------------------------------------------------------------------

    def _cleanup_stale_processes(self):
        """Kill leftover Tor pool processes from a previous run."""
        killed = 0

        # 1. Kill from saved PID file
        if PID_FILE.exists():
            try:
                saved = json.loads(PID_FILE.read_text())
                for entry in saved:
                    pid = entry.get("pid", 0)
                    if pid and _kill_pid(pid, f"saved instance {entry.get('index', '?')}"):
                        killed += 1
                PID_FILE.unlink(missing_ok=True)
            except Exception as e:
                log.debug(f"  PID file cleanup error: {e}")

        # 2. Kill anything on pool port range
        for i in range(self.pool_size):
            for port in (self.base_socks_port + i, self.base_socks_port + 100 + i):
                if _is_port_in_use(port):
                    pid = _get_pid_on_port(port)
                    if pid and _kill_pid(pid, f"port {port}"):
                        killed += 1

        if killed:
            time.sleep(2)
            log.info(f"  Cleaned up {killed} stale Tor pool process(es)")

    def _save_pids(self):
        """Write current pool PIDs to disk for crash recovery."""
        entries = []
        for inst in self.instances:
            if inst.process and inst.process.poll() is None:
                entries.append({
                    "index": inst.index,
                    "pid": inst.process.pid,
                    "socks_port": inst.socks_port,
                    "control_port": inst.control_port,
                })
        try:
            PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            PID_FILE.write_text(json.dumps(entries, indent=2))
        except Exception as e:
            log.debug(f"  Failed to save PIDs: {e}")

    def _register_cleanup(self):
        """Register atexit and signal handlers so pool.stop() runs on exit."""
        atexit.register(self.stop)

        def _signal_handler(sig, frame):
            log.info("Signal received — stopping Tor pool...")
            self.stop()
            # Re-raise so Python's default handler can exit
            signal.signal(sig, signal.SIG_DFL)
            os.kill(os.getpid(), sig)

        # Only register signal handlers if we're in the main thread
        if threading.current_thread() is threading.main_thread():
            try:
                signal.signal(signal.SIGINT, _signal_handler)
                signal.signal(signal.SIGTERM, _signal_handler)
            except (OSError, ValueError):
                pass  # Can't set signal handlers in some contexts

    def _generate_torrc(self, instance: TorInstance) -> str:
        """
        Generate a torrc for this instance based on the base torrc.

        Reads the base torrc, strips instance-specific lines (SocksPort,
        ControlPort, DataDirectory, Log), keeps Bridge lines and transport
        plugins, and adds instance-specific settings.
        """
        if not TORRC_TEMPLATE.exists():
            raise FileNotFoundError(f"Base torrc not found: {TORRC_TEMPLATE}")

        base_lines = TORRC_TEMPLATE.read_text().splitlines()
        kept_lines = []
        strip_keys = {"socksport", "controlport", "datadirectory", "log"}

        for line in base_lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                kept_lines.append(line)
                continue
            key = stripped.split()[0].lower() if stripped.split() else ""
            if key in strip_keys:
                continue
            # Convert relative paths to absolute for transport plugins and GeoIP
            if key == "clienttransportplugin":
                # e.g. "ClientTransportPlugin obfs4 exec ./tor/pluggable_transports/lyrebird.exe"
                parts = stripped.split()
                for i, part in enumerate(parts):
                    if part.startswith("./"):
                        parts[i] = str(TOR_BUNDLE_DIR / part[2:])
                kept_lines.append(" ".join(parts))
            elif key in ("geoipfile", "geoipv6file"):
                parts = stripped.split(maxsplit=1)
                if len(parts) == 2 and parts[1].startswith("./"):
                    parts[1] = str(TOR_BUNDLE_DIR / parts[1][2:])
                kept_lines.append(" ".join(parts))
            else:
                kept_lines.append(line)

        # Add instance-specific settings
        data_dir_str = str(instance.data_dir).replace("\\", "/")
        log_path_str = str(instance.log_path).replace("\\", "/")
        kept_lines.extend([
            "",
            f"# Pool instance {instance.index}",
            f"SocksPort {instance.socks_port}",
            f"ControlPort {instance.control_port}",
            f"DataDirectory {data_dir_str}",
            f"Log notice file {log_path_str}",
        ])

        torrc_content = "\n".join(kept_lines)
        instance.torrc_path.write_text(torrc_content)
        return torrc_content

    def _seed_data_dir(self, data_dir: Path):
        """
        Copy cached descriptors from the main Tor instance to speed up bootstrap.

        Without seeding, pool instances must download the full consensus and
        relay descriptors via obfs4 bridges from scratch — which can take 3-5+
        minutes. Seeding from the already-bootstrapped main instance cuts this
        to ~10-30 seconds.
        """
        main_data = TOR_BUNDLE_DIR / "tor-data"
        if not main_data.exists():
            return

        seed_files = [
            "cached-certs",
            "cached-microdesc-consensus",
            "cached-microdescs",
            "cached-microdescs.new",
            "cached-descriptors",
            "cached-descriptors.new",
        ]

        seeded = 0
        for fname in seed_files:
            src = main_data / fname
            if src.exists():
                dst = data_dir / fname
                # Only seed if the dest doesn't exist or is older
                if not dst.exists() or src.stat().st_mtime > dst.stat().st_mtime:
                    try:
                        shutil.copy2(str(src), str(dst))
                        seeded += 1
                    except Exception:
                        pass

        if seeded:
            log.debug(f"  Seeded {seeded} cache files to {data_dir.name}")

    def start(self):
        """Generate torrc files and launch N tor.exe processes."""
        POOL_DATA_DIR.mkdir(parents=True, exist_ok=True)

        tor_exe = TOR_BUNDLE_DIR / "tor" / "tor.exe"
        if not tor_exe.exists():
            raise FileNotFoundError(f"tor.exe not found: {tor_exe}")

        # Kill any stale pool processes from a previous run
        self._cleanup_stale_processes()

        now = time.time()

        for i in range(self.pool_size):
            socks_port = self.base_socks_port + i
            control_port = self.base_socks_port + 100 + i

            data_dir = POOL_DATA_DIR / f"instance-{i}"
            data_dir.mkdir(parents=True, exist_ok=True)

            # Remove stale lock from previous crashed run
            lock_file = data_dir / "lock"
            if lock_file.exists():
                try:
                    lock_file.unlink()
                except Exception:
                    pass

            # Seed cached descriptors from main instance for fast bootstrap
            self._seed_data_dir(data_dir)

            torrc_path = TOR_BUNDLE_DIR / f"torrc-pool-{i}"
            log_path = POOL_DATA_DIR / f"instance-{i}.log"

            instance = TorInstance(
                index=i,
                socks_port=socks_port,
                control_port=control_port,
                data_dir=data_dir,
                torrc_path=torrc_path,
                log_path=log_path,
                last_progress_at=now,
                _last_control_ok=now,
            )

            try:
                self._generate_torrc(instance)

                # Launch tor process
                proc = subprocess.Popen(
                    [str(tor_exe), "-f", str(torrc_path)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW
                    if os.name == "nt" else 0,
                )
                instance.process = proc
                instance.state = InstanceState.BOOTSTRAPPING
                log.info(f"  Tor pool instance {i}: PID {proc.pid} "
                         f"(SOCKS:{socks_port}, Control:{control_port})")

            except Exception as e:
                instance.state = InstanceState.FAILED
                instance.last_error = str(e)
                log.error(f"  Tor pool instance {i}: failed to start: {e}")

            self.instances.append(instance)

        # Save PIDs for crash recovery and register cleanup handlers
        self._save_pids()
        self._register_cleanup()

        # Start health monitor thread
        self._stop_event.clear()
        self._last_summary_log = time.time()
        self._monitor_thread = threading.Thread(
            target=self._health_monitor, daemon=True, name="tor-pool-monitor"
        )
        self._monitor_thread.start()

    def wait_ready(self, timeout: int = None) -> int:
        """
        Block until at least 1 instance is bootstrapped.
        Returns count of ready instances, or 0 if timeout.
        """
        if timeout is None:
            timeout = self.bootstrap_timeout

        deadline = time.time() + timeout
        last_log = 0

        while time.time() < deadline:
            ready = [i for i in self.instances if i.state == InstanceState.READY]
            if ready:
                log.info(f"  Tor pool: {len(ready)}/{self.pool_size} instances ready")
                return len(ready)

            # Log progress every 10 seconds
            now = time.time()
            if now - last_log >= 10:
                bootstrapping = [
                    i for i in self.instances
                    if i.state == InstanceState.BOOTSTRAPPING
                ]
                if bootstrapping:
                    pcts = ", ".join(
                        f"#{i.index}:{i.bootstrap_pct}%"
                        for i in bootstrapping
                    )
                    log.info(f"  Tor pool bootstrapping: {pcts}")
                last_log = now

            # Check if all have permanently failed (FAILED with exhausted restarts,
            # or STALLED with exhausted restarts)
            all_terminal = all(
                (i.state == InstanceState.FAILED and i.restart_count >= self.max_restarts)
                or (i.state == InstanceState.STALLED and i.restart_count >= self.max_restarts)
                for i in self.instances
            )
            if all_terminal:
                log.error("  Tor pool: all instances failed (restarts exhausted)")
                return 0

            time.sleep(2)

        log.warning(f"  Tor pool: timeout after {timeout}s, "
                    f"{len(self.get_healthy())} ready")
        return len(self.get_healthy())

    def get_healthy(self) -> list[TorInstance]:
        """Return list of instances in READY state."""
        return [i for i in self.instances if i.state == InstanceState.READY]

    def get_raceable(self, cooldown: int = 300) -> list[TorInstance]:
        """Return READY instances not in login wall cooldown.

        Falls back to get_healthy() if all are in cooldown — better to
        retry than skip entirely.
        """
        now = time.time()
        raceable = [
            i for i in self.instances
            if i.state == InstanceState.READY
            and (now - i.last_login_wall_at) > cooldown
        ]
        return raceable if raceable else self.get_healthy()

    def record_login_wall(self, instance: TorInstance):
        """Record a login wall hit: set cooldown and fire background NEWNYM."""
        instance.last_login_wall_at = time.time()
        instance.probe_failures += 1
        log.info(f"  Login wall on instance {instance.index} — "
                 f"cooldown 5min, NEWNYM in background")
        threading.Thread(
            target=self.renew_circuit, args=(instance,),
            daemon=True, name=f"newnym-{instance.index}",
        ).start()

    def renew_circuit(self, instance: TorInstance) -> bool:
        """Send SIGNAL NEWNYM to a specific instance's control port."""
        try:
            sock = socket.create_connection(
                ("127.0.0.1", instance.control_port), timeout=10
            )
            if self.control_password:
                sock.sendall(
                    f'AUTHENTICATE "{self.control_password}"\r\n'.encode()
                )
            else:
                sock.sendall(b"AUTHENTICATE\r\n")
            response = sock.recv(256).decode()
            if "250" not in response:
                sock.close()
                return False

            sock.sendall(b"SIGNAL NEWNYM\r\n")
            response = sock.recv(256).decode()
            sock.close()
            return "250" in response

        except Exception as e:
            log.debug(f"  Circuit renewal failed on instance {instance.index}: {e}")
            return False

    def record_probe_result(self, instance: TorInstance, success: bool, duration: float):
        """Record probe outcome on an instance. Thread-safe under CPython GIL."""
        if success:
            instance.probe_successes += 1
            instance.last_probe_time = duration
        else:
            instance.probe_failures += 1

    def stop(self):
        """Terminate all Tor processes and clean up."""
        if self._stop_event.is_set() and not self.instances:
            return  # Already stopped

        self._stop_event.set()

        for instance in self.instances:
            if instance.process and instance.process.poll() is None:
                try:
                    instance.process.terminate()
                    instance.process.wait(timeout=5)
                except Exception:
                    try:
                        instance.process.kill()
                    except Exception:
                        pass
                log.debug(f"  Tor pool instance {instance.index}: stopped")

        self.instances.clear()

        # Remove PID file — we've cleaned up our own processes
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

        log.info("Tor pool stopped")

    def _query_bootstrap_pct(self, instance: TorInstance) -> int | None:
        """
        Query bootstrap progress via the control port.
        Returns percentage (0-100) or None if unreachable.
        """
        try:
            sock = socket.create_connection(
                ("127.0.0.1", instance.control_port), timeout=5
            )
            if self.control_password:
                sock.sendall(
                    f'AUTHENTICATE "{self.control_password}"\r\n'.encode()
                )
            else:
                sock.sendall(b"AUTHENTICATE\r\n")
            auth_resp = sock.recv(256).decode()

            if "250" not in auth_resp:
                sock.close()
                return None

            sock.sendall(b"GETINFO status/bootstrap-phase\r\n")
            status_resp = sock.recv(512).decode()
            sock.close()

            if "PROGRESS=" in status_resp:
                pct_str = status_resp.split("PROGRESS=")[1].split()[0]
                return int(pct_str)

            return None

        except (ConnectionRefusedError, OSError):
            return None
        except Exception as e:
            log.debug(f"  Bootstrap query instance {instance.index}: {e}")
            return None

    def _restart_instance(self, instance: TorInstance):
        """
        Kill and relaunch a Tor instance. The core self-healing primitive.

        1. Kill process (terminate, wait 5s, fallback to kill)
        2. Sleep 0.5s for Windows file handle release
        3. Clear lock file
        4. Re-seed cache from main instance
        5. Regenerate torrc (same ports)
        6. Relaunch tor.exe
        7. Reset state to BOOTSTRAPPING
        8. Increment restart_count
        """
        idx = instance.index
        log.info(f"  Tor pool instance {idx}: restarting "
                 f"(attempt {instance.restart_count + 1}/{self.max_restarts})")

        # Kill existing process
        if instance.process and instance.process.poll() is None:
            try:
                instance.process.terminate()
                instance.process.wait(timeout=5)
            except Exception:
                try:
                    instance.process.kill()
                except Exception:
                    pass

        # Also kill anything else holding our ports (stale processes)
        for port in (instance.socks_port, instance.control_port):
            if _is_port_in_use(port):
                pid = _get_pid_on_port(port)
                if pid:
                    _kill_pid(pid, f"stale on port {port}")

        # Windows file handle release
        time.sleep(1.0)

        # Clear lock file
        lock_file = instance.data_dir / "lock"
        if lock_file.exists():
            try:
                lock_file.unlink()
            except Exception:
                pass

        # Re-seed cache from main instance
        self._seed_data_dir(instance.data_dir)

        # Regenerate torrc (same ports)
        try:
            self._generate_torrc(instance)
        except Exception as e:
            instance.state = InstanceState.FAILED
            instance.last_error = f"torrc generation failed: {e}"
            instance.restart_count += 1
            log.error(f"  Tor pool instance {idx}: torrc generation failed: {e}")
            return

        # Relaunch tor.exe
        tor_exe = TOR_BUNDLE_DIR / "tor" / "tor.exe"
        try:
            proc = subprocess.Popen(
                [str(tor_exe), "-f", str(instance.torrc_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW
                if os.name == "nt" else 0,
            )
            instance.process = proc
            now = time.time()
            instance.state = InstanceState.BOOTSTRAPPING
            instance.bootstrap_pct = 0
            instance.last_progress_at = now
            instance._last_control_ok = now
            instance.last_error = ""
            instance.restart_count += 1
            log.info(f"  Tor pool instance {idx}: relaunched PID {proc.pid}")
            self._save_pids()

        except Exception as e:
            instance.state = InstanceState.FAILED
            instance.last_error = str(e)
            instance.restart_count += 1
            log.error(f"  Tor pool instance {idx}: relaunch failed: {e}")

    def _log_health_summary(self):
        """Log a one-line summary with state counts, restarts, and probe stats."""
        state_counts = {}
        total_restarts = 0
        probe_parts = []
        now = time.time()

        for inst in self.instances:
            state_counts[inst.state.value] = state_counts.get(inst.state.value, 0) + 1
            total_restarts += inst.restart_count

            total_probes = inst.probe_successes + inst.probe_failures
            if total_probes > 0:
                pct = int(100 * inst.probe_successes / total_probes)
                lw = ""
                if inst.last_login_wall_at > 0:
                    ago = int(now - inst.last_login_wall_at)
                    lw = f" LW:{ago}s ago"
                probe_parts.append(
                    f"#{inst.index}={inst.probe_successes}/{total_probes} ({pct}%){lw}"
                )
            else:
                probe_parts.append(f"#{inst.index}=no probes")

        states_str = ", ".join(
            f"{count} {state}" for state, count in state_counts.items()
        )
        probes_str = ", ".join(probe_parts)

        log.info(f"  Tor pool health: [{states_str}] "
                 f"restarts: {total_restarts}, probes: {probes_str}")

    def _health_monitor(self):
        """
        Background daemon thread: check health every 10s.

        Each cycle:
        a) Per-instance checks: crash detection, stall detection, bootstrap tracking
        b) Auto-restart: one stalled/failed instance per cycle (prevents thundering herd)
        c) Periodic summary: log health stats every 60s
        """
        while not self._stop_event.is_set():
            now = time.time()

            # --- (a) Per-instance checks ---
            for instance in self.instances:
                # Skip already-known FAILED instances (waiting for restart)
                if instance.state == InstanceState.FAILED:
                    continue

                # Crash detection — only logs once (sets state to FAILED)
                if instance.process and instance.process.poll() is not None:
                    exit_code = instance.process.returncode
                    instance.state = InstanceState.FAILED
                    instance.last_error = f"Process exited with code {exit_code}"
                    log.warning(f"  Tor pool instance {instance.index}: "
                                f"crashed (exit code {exit_code})")
                    continue

                if instance.state == InstanceState.BOOTSTRAPPING:
                    pct = self._query_bootstrap_pct(instance)
                    if pct is not None:
                        if pct != instance.bootstrap_pct:
                            instance.bootstrap_pct = pct
                            instance.last_progress_at = now
                        if pct >= 100:
                            instance.state = InstanceState.READY
                            instance._last_control_ok = now
                            log.info(f"  Tor pool instance {instance.index}: "
                                     f"bootstrapped (SOCKS:{instance.socks_port})")
                        elif (now - instance.last_progress_at) > self.stall_timeout:
                            instance.state = InstanceState.STALLED
                            instance.last_error = f"Stalled at {pct}% for {self.stall_timeout}s"
                            log.warning(f"  Tor pool instance {instance.index}: "
                                        f"stalled at {pct}% bootstrap")

                elif instance.state == InstanceState.READY:
                    pct = self._query_bootstrap_pct(instance)
                    if pct is not None:
                        instance._last_control_ok = now
                    elif (now - instance._last_control_ok) > self.control_timeout:
                        instance.state = InstanceState.STALLED
                        instance.last_error = (
                            f"Control port unreachable for {self.control_timeout}s"
                        )
                        log.warning(f"  Tor pool instance {instance.index}: "
                                    f"control port unreachable, marking stalled")

            # --- (b) Auto-restart (one per cycle to prevent thundering herd) ---
            restarted_this_cycle = False

            # Restart STALLED first (alive but useless), then FAILED
            for instance in self.instances:
                if restarted_this_cycle:
                    break
                if (instance.state in (InstanceState.STALLED, InstanceState.FAILED)
                        and instance.restart_count < self.max_restarts):
                    self._restart_instance(instance)
                    restarted_this_cycle = True

            # --- (c) Periodic summary (every 60s) ---
            if (now - self._last_summary_log) >= 60:
                self._log_health_summary()
                self._last_summary_log = now

            self._stop_event.wait(10)
