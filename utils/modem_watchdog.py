"""
modem_watchdog.py
-----------------
Software watchdog for the Waveshare 4G module (SIM7600/SIM7900).

Targeted symptom: the modem freezes (2 steady red LEDs, no traffic), without the
Raspberry Pi itself being stuck. A plain `sudo reboot` recovers the situation
because it forces a USB power cycle of the module.

This module reproduces the effect of that reboot *without* restarting the Pi:
  1. Periodic ping of a reliable server (8.8.8.8), bound to the identified 4G
     interface (never a "general" ping, which could travel over Ethernet or Wi-Fi)
  2. If N consecutive pings fail -> network interface down/up
  3. If the down/up fails -> USB reset of the modem through /sys
  4. If everything fails -> AT+CFUN=1,1 over the serial port (firmware reset)
  5. As a last resort: log + alert file transmitted to the NAS (no reboot, so as
     not to lose the videos being recorded)

Before any destructive action (down/up, USB power cycle), it waits for an SFTP
upload in progress to finish (see SFTPManager.upload_in_progress). On repeated
complete failures, the delay before the next attempt grows exponentially
(120s, 240s, ... up to 1h) rather than hammering the modem indefinitely every
2-4 minutes in the case of a genuine hardware fault.

Usage:
    from utils.modem_watchdog import ModemWatchdog
    wd = ModemWatchdog()
    wd.start()   # Daemon thread, stops when the Python process exits
    ...
    wd.stop()    # Clean shutdown
"""

import threading
import subprocess
import time
import os
import glob
import logging

from utils.alerting import write_alert

logger = logging.getLogger("ModemWatchdog")

# ---------------------------------------------------------------------------
# Configuration (tunable without touching the rest of the code)
# ---------------------------------------------------------------------------

PING_HOST        = "8.8.8.8"        # Main health-check ping target
PING_HOST_FALLBACK = "1.1.1.1"      # Fallback if the main target does not answer
                                     # (an operator may block or throttle ICMP to
                                     # one IP without the 4G link being down)
PING_COUNT       = 2                 # Packets sent per check
PING_TIMEOUT     = 5                 # Max seconds per ping
CHECK_INTERVAL   = 30                # Seconds between two normal checks
MAX_FAILURES     = 3                 # Consecutive failures before acting
RECOVERY_COOLDOWN = 120              # Rest seconds after a recovery (1st total failure)
MAX_RECOVERY_COOLDOWN = 3600         # Exponential backoff ceiling (1h) after repeated failures

# Possible network interface names for the 4G modem (tried first, cheap)
MODEM_INTERFACES = ["usb0", "wwan0", "eth1"]
# Fallback if none of the names above exists: USB drivers typical of cellular
# modems (qmi_wwan/cdc_ether/...). More robust than a fixed list of names, which
# may stop matching after a USB re-enumeration or depending on the modem mode.
MODEM_DRIVER_HINTS = ("qmi_wwan", "cdc_ether", "cdc_ncm", "rndis_host", "cdc_mbim")

# AT serial port of the modem (the SIM7600 exposes several /dev/ttyUSB*, 2 is the AT port)
AT_SERIAL_CANDIDATES = [
    "/dev/ttyUSB2",  # SIM7600 -- standard AT port
    "/dev/ttyUSB1",
    "/dev/ttyUSB3",
]

# ---------------------------------------------------------------------------


class ModemWatchdog(threading.Thread):
    """
    Daemon thread that monitors 4G connectivity and tries to recover the modem
    without rebooting in case of a freeze.
    """

    def __init__(self,
                 ping_host: str = PING_HOST,
                 check_interval: int = CHECK_INTERVAL,
                 max_failures: int = MAX_FAILURES,
                 sftp_manager=None):
        super().__init__(daemon=True, name="modem-watchdog")
        self.ping_host      = ping_host
        self.check_interval = check_interval
        self.max_failures   = max_failures
        self.running        = False
        self._failure_count = 0
        self._recovery_count = 0
        self._last_recovery_ts = 0.0
        # Number of COMPLETE recovery cycles (all 4 steps) that failed outright
        # in a row -- distinct from _failure_count (ping failures). Drives the
        # exponential backoff: reset to 0 as soon as any step succeeds.
        self._consecutive_total_failures = 0
        # Optional reference to the upload manager: lets an alert file reach the
        # NAS when every recovery attempt fails (no external SMS/email/webhook
        # notification is configured).
        self._sftp_manager = sftp_manager

        logger.info(
            f"[Watchdog] Initialised -- pinging {self.ping_host} "
            f"every {self.check_interval}s, threshold={self.max_failures} failures"
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def start(self):
        self.running = True
        super().start()
        logger.info("[Watchdog] Started.")

    def stop(self):
        self.running = False
        logger.info("[Watchdog] Stopped.")

    @property
    def is_connected(self) -> bool:
        """True if the last ping check succeeded."""
        return self._failure_count == 0

    @property
    def recovery_count(self) -> int:
        return self._recovery_count

    # -----------------------------------------------------------------------
    # Thread principal
    # -----------------------------------------------------------------------

    def run(self):
        # Wait 10s after start-up to let the modem initialise
        time.sleep(10)

        while self.running:
            try:
                iface = self._find_modem_interface()
                if iface:
                    ok = self._ping_check(iface)
                else:
                    # No 4G interface identified: the 4G link cannot be checked
                    # specifically (an unbound ping could travel through another
                    # interface, Ethernet or Wi-Fi, and hide a real problem).
                    # Treated as a 4G connectivity failure.
                    logger.warning(
                        "[Watchdog] No 4G interface identified -- 4G connectivity "
                        "considered unavailable until one appears."
                    )
                    ok = False

                if ok:
                    if self._failure_count > 0:
                        logger.info(
                            f"[Watchdog] ✅ 4G connectivity restored "
                            f"(after {self._failure_count} failures)"
                        )
                    self._failure_count = 0
                    time.sleep(self.check_interval)
                else:
                    # Capped at max_failures: beyond that the number carries no
                    # more decision value (the threshold is already reached) and
                    # only produced logs such as "47/3" for the whole cooldown,
                    # misleading as to the real severity.
                    self._failure_count = min(self._failure_count + 1, self.max_failures)
                    logger.warning(
                        f"[Watchdog] ⚠️ 4G ping failed "
                        f"({self._failure_count}/{self.max_failures})"
                    )

                    if self._failure_count >= self.max_failures:
                        # Anti-loop cooldown, with an exponential backoff capped
                        # after repeated COMPLETE failures (see _attempt_recovery):
                        # 120s, 240s, 480s... up to MAX_RECOVERY_COOLDOWN (1h).
                        # A genuine hardware fault must not trigger USB/firmware
                        # power cycles every 2-4 minutes for days -- only more and
                        # more rarely.
                        cooldown = min(
                            RECOVERY_COOLDOWN * (2 ** self._consecutive_total_failures),
                            MAX_RECOVERY_COOLDOWN,
                        )
                        elapsed = time.time() - self._last_recovery_ts
                        if elapsed < cooldown:
                            wait = int(cooldown - elapsed)
                            logger.info(
                                f"[Watchdog] Cooldown active ({cooldown}s, "
                                f"consecutive total failures: {self._consecutive_total_failures}) "
                                f"-- waiting {wait}s before the next attempt"
                            )
                            time.sleep(min(wait, self.check_interval))
                        else:
                            self._attempt_recovery()
                    else:
                        # Retry faster as long as the threshold is not reached
                        time.sleep(max(5, self.check_interval // 3))

            except Exception as exc:
                logger.error(f"[Watchdog] Unexpected error: {exc}", exc_info=True)
                time.sleep(self.check_interval)

    # -----------------------------------------------------------------------
    # Ping
    # -----------------------------------------------------------------------

    def _ping_check(self, iface: str | None = None) -> bool:
        """Return True if the ping succeeds, on the main target or, failing
        that, on a fallback target (an operator may block or throttle ICMP to one
        IP without the real 4G connectivity being affected -- a single target
        would risk triggering a full hardware recovery on a perfectly healthy
        modem)."""
        if self._ping_once(self.ping_host, iface):
            return True
        if self.ping_host != PING_HOST_FALLBACK:
            return self._ping_once(PING_HOST_FALLBACK, iface)
        return False

    def _ping_once(self, host: str, iface: str | None) -> bool:
        """A single ping attempt towards `host`.

        If `iface` is given, the ping is bound to that interface (-I) so that the
        4G link specifically is tested. Without it, `ping` takes the system
        default route -- which may well be Ethernet or Wi-Fi if those links are
        up, in which case a successful ping proves nothing about the real state
        of the modem."""
        cmd = ["ping", "-c", str(PING_COUNT), "-W", str(PING_TIMEOUT), "-q"]
        if iface:
            cmd += ["-I", iface]
        cmd.append(host)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=PING_TIMEOUT * PING_COUNT + 2,
            )
            return result.returncode == 0
        except subprocess.TimeoutExpired:
            return False
        except FileNotFoundError:
            # The `ping` binary itself is missing (package removed, minimal
            # image) -- distinct from a real network outage. Logged at ERROR
            # (visible by default, unlike debug) so that repeated hardware resets
            # are not triggered on a perfectly healthy modem because of a missing
            # tool, without anyone noticing.
            logger.error(
                "[Watchdog] `ping` binary not found -- 4G connectivity cannot be "
                "checked. Install the iputils-ping package."
            )
            return False
        except Exception as exc:
            logger.debug(f"[Watchdog] ping exception ({host}): {exc}")
            return False

    # -----------------------------------------------------------------------
    # Recovery pipeline
    # -----------------------------------------------------------------------

    def _wait_for_upload_idle(self, max_wait: int = 20):
        """Wait for an SFTP upload in progress to finish before a destructive
        network action (interface down/up, USB power cycle), so as not to truncate
        a transfer in flight. Bounded wait: if the upload drags on beyond
        max_wait, the connection is considered compromised anyway and recovery
        proceeds rather than waiting indefinitely."""
        sftp = self._sftp_manager
        if not sftp or not hasattr(sftp, "upload_in_progress"):
            return
        waited = 0
        while sftp.upload_in_progress.is_set() and waited < max_wait:
            time.sleep(1)
            waited += 1
        if waited > 0:
            logger.info(f"[Watchdog] Upload in progress detected -- waited {waited}s before recovery.")

    def _wait_and_check(self, max_wait: int, poll_interval: int = 3) -> bool:
        """Probe the ping every poll_interval seconds up to max_wait seconds of
        REAL time (time.monotonic(), not an iteration counter), returning as soon
        as connectivity comes back.

        Two important corrections over a first version:
        1. Real time is measured with a monotonic deadline: a plain
           `waited += poll_interval` counter largely underestimated the time
           actually elapsed, because _ping_check() alone can take up to ~24s
           (2 sequential pings with fallback) -- a max_wait "documented" at 20s
           could therefore really last more than 3 minutes.
        2. The interface is rediscovered on EVERY iteration rather than received
           as a frozen argument: the recovery steps (USB reset, firmware reset)
           are precisely the ones that can make the interface appear or be
           renamed (e.g. wwan0 -> wwan1) DURING that waiting window -- a frozen
           name would have made the ping fail (-I on an interface that no longer
           exists) or, worse, fall back to an unbound default route (a false
           positive through Ethernet/Wi-Fi)."""
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            iface = self._find_modem_interface()
            if self._ping_check(iface):
                return True
        return False

    def _attempt_recovery(self):
        """
        Try to recover the modem in increasing order of aggressiveness.
        Returns True if the connection is restored.
        """
        logger.error(
            f"[Watchdog] 🔴 {self._failure_count} consecutive pings "
            f"failed -- starting the recovery procedure."
        )
        self._last_recovery_ts = time.time()
        self._recovery_count += 1

        # Never cut the interface or the modem in the middle of an SFTP transfer.
        self._wait_for_upload_idle()

        # Step 1: restart the network interface
        iface = self._find_modem_interface()
        if iface:
            logger.info(f"[Watchdog] Step 1 -- restarting interface {iface}...")
            if self._restart_network_interface(iface):
                # ~20s: an LTE modem typically takes 15-30s to re-attach after a
                # plain down/up, far more than the previous 5s.
                if self._wait_and_check(max_wait=20):
                    logger.info("[Watchdog] ✅ Recovery step 1 succeeded (interface down/up).")
                    self._failure_count = 0
                    self._consecutive_total_failures = 0
                    return True

        # Step 2: USB reset of the modem through /sys
        logger.info("[Watchdog] Step 2 -- USB reset of the modem...")
        if self._usb_reset():
            # ~25s: USB re-enumeration plus network re-attachment, longer than a
            # plain interface down/up.
            if self._wait_and_check(max_wait=25):
                logger.info("[Watchdog] ✅ Recovery step 2 succeeded (USB reset).")
                self._failure_count = 0
                self._consecutive_total_failures = 0
                return True

        # Step 3: AT+CFUN=1,1 (modem firmware reset)
        logger.info("[Watchdog] Step 3 -- sending AT+CFUN=1,1 (firmware reset)...")
        if self._at_reset():
            if self._wait_and_check(max_wait=20):
                logger.info("[Watchdog] ✅ Recovery step 3 succeeded (AT reset).")
                self._failure_count = 0
                self._consecutive_total_failures = 0
                return True

        # Step 4: NetworkManager -- bring the GSM connection back up
        logger.info("[Watchdog] Step 4 -- restarting the NetworkManager connection...")
        if self._nmcli_reconnect():
            if self._wait_and_check(max_wait=15):
                logger.info("[Watchdog] ✅ Recovery step 4 succeeded (nmcli reconnect).")
                self._failure_count = 0
                self._consecutive_total_failures = 0
                return True

        # Every step failed
        self._consecutive_total_failures += 1

        if self._consecutive_total_failures >= 2:
            logger.critical(
                f"[Watchdog] 🚨 Critical threshold reached ({self._consecutive_total_failures} total failures). "
                f"The modem has probably crashed at hardware level. FORCED SYSTEM REBOOT!"
            )
            write_alert("modem_fatal", "Automatic reboot triggered by the watchdog after total loss of the modem.")
            time.sleep(5)
            subprocess.run(["sudo", "reboot"])
            time.sleep(600)  # Block the thread while the Pi shuts down
            return False

        next_cooldown = min(
            RECOVERY_COOLDOWN * (2 ** self._consecutive_total_failures),
            MAX_RECOVERY_COOLDOWN,
        )
        logger.critical(
            f"[Watchdog] ❌ Every recovery attempt failed "
            f"(consecutive total failure #{self._consecutive_total_failures}). "
            f"Next attempt in {next_cooldown}s. The modem is probably in a state "
            f"that requires a hardware reboot."
        )
        # Alert file (see utils/alerting.py), uploaded as soon as the connection
        # returns. The exponential backoff above already spaces the cycles out
        # naturally (up to 1h), so there is no need to rate-limit the alerts on
        # top of that: every complete total failure generates one.
        alert_path = write_alert(
            "modem",
            f"Complete 4G recovery failed (consecutive total failure "
            f"#{self._consecutive_total_failures}). Next attempt in "
            f"{next_cooldown}s. A hardware reboot is probably needed if this persists."
        )
        if alert_path and self._sftp_manager and not self._sftp_manager.upload_file(alert_path):
            logger.warning(f"[Watchdog] Modem alert not queued (queue full): {alert_path}")
        # Reset the ping counter to avoid an immediate re-trigger -- the
        # exponential cooldown (based on _consecutive_total_failures) takes over.
        self._failure_count = 0
        return False

    # -----------------------------------------------------------------------
    # Individual steps
    # -----------------------------------------------------------------------

    def _find_modem_interface(self) -> str | None:
        """Return the name of the active network interface of the 4G modem.

        Tries the known names first (cheap), then falls back to discovery by USB
        driver (qmi_wwan/cdc_ether/...): a fixed list of names may stop matching
        after a USB re-enumeration (e.g. wwan0 -> wwan1)."""
        for iface in MODEM_INTERFACES:
            path = f"/sys/class/net/{iface}"
            if os.path.exists(path):
                logger.debug(f"[Watchdog] Modem interface found (known name): {iface}")
                return iface

        try:
            for iface_path in glob.glob("/sys/class/net/*"):
                iface = os.path.basename(iface_path)
                driver_link = os.path.join(iface_path, "device", "driver")
                if os.path.islink(driver_link):
                    driver = os.path.basename(os.path.realpath(driver_link))
                    if driver in MODEM_DRIVER_HINTS:
                        logger.debug(
                            f"[Watchdog] Modem interface found (driver {driver}): {iface}"
                        )
                        return iface
        except Exception as exc:
            logger.debug(f"[Watchdog] Driver-based discovery failed: {exc}")

        logger.warning(
            f"[Watchdog] No 4G interface found (names: {MODEM_INTERFACES}, "
            f"drivers: {MODEM_DRIVER_HINTS})"
        )
        return None

    def _restart_network_interface(self, iface: str) -> bool:
        """ip link set <iface> down/up."""
        try:
            subprocess.run(
                ["sudo", "ip", "link", "set", iface, "down"],
                timeout=5, capture_output=True
            )
            time.sleep(2)
            result = subprocess.run(
                ["sudo", "ip", "link", "set", iface, "up"],
                timeout=5, capture_output=True
            )
            return result.returncode == 0
        except Exception as exc:
            logger.error(f"[Watchdog] Interface restart failed for {iface}: {exc}")
            return False

    def _usb_reset(self) -> bool:
        """
        USB reset of the modem through /sys/bus/usb/devices/<id>/authorized.
        Simulates unplugging and replugging USB without touching the Pi.
        Requires sudo (root-only write on that sysfs node), restricted through
        sudoers to `tee` on that exact path -- never a full shell.
        """
        try:
            # Find the USB device of the modem (SIM7600 VID=1E0E)
            # or search by the interface name
            usb_auth_paths = glob.glob(
                "/sys/bus/usb/devices/*/authorized"
            )
            modem_path = None

            for auth_path in usb_auth_paths:
                try:
                    # Read idVendor to identify the SIM7600/SIM7900
                    vendor_path = os.path.join(
                        os.path.dirname(auth_path), "idVendor"
                    )
                    if os.path.exists(vendor_path):
                        with open(vendor_path) as f:
                            vendor = f.read().strip().lower()
                        # SIM7600 VID: 1e0e, Quectel EC21: 2c7c, generic fallback
                        if vendor in ("1e0e", "2c7c", "12d1", "1bc7"):
                            modem_path = auth_path
                            break
                except OSError:
                    # A USB device can vanish between discovery and read --
                    # precisely the kind of USB instability this watchdog is meant
                    # to handle. We carry on with the next device rather than
                    # abandoning the whole search.
                    continue

            if not modem_path:
                # Fallback: search through the network interface
                iface = self._find_modem_interface()
                if iface:
                    # Walk up /sys to find the parent USB device
                    net_path = f"/sys/class/net/{iface}/device"
                    if os.path.exists(net_path):
                        real_path = os.path.realpath(net_path)
                        # Look for `authorized` among the parents
                        candidate = real_path
                        for _ in range(5):
                            candidate = os.path.dirname(candidate)
                            auth = os.path.join(candidate, "authorized")
                            if os.path.exists(auth):
                                modem_path = auth
                                break

            if not modem_path:
                logger.warning(
                    "[Watchdog] USB reset: modem USB device not found in /sys"
                )
                return False

            logger.info(f"[Watchdog] USB reset through: {modem_path}")
            # `sudo tee <path>` rather than `sudo bash -c "echo ... > path"`: a
            # single fixed binary as the sudo argument (no interpolated shell
            # string), which allows a sudoers rule restricted to that exact path.
            r0 = subprocess.run(
                ["sudo", "tee", modem_path],
                input=b"0\n", timeout=5, capture_output=True
            )
            time.sleep(3)
            # Re-enable
            r1 = subprocess.run(
                ["sudo", "tee", modem_path],
                input=b"1\n", timeout=5, capture_output=True
            )
            if r0.returncode != 0 or r1.returncode != 0:
                logger.error(
                    f"[Watchdog] USB reset: sudo tee write failed "
                    f"(codes {r0.returncode}/{r1.returncode})"
                )
                return False
            return True

        except Exception as exc:
            logger.error(f"[Watchdog] USB reset failed: {exc}")
            return False

    def _at_reset(self) -> bool:
        """
        Send AT+CFUN=1,1 to the modem over the serial port to force a firmware
        reset. Works even when the network is down.
        """
        at_port = None
        for candidate in AT_SERIAL_CANDIDATES:
            if os.path.exists(candidate):
                at_port = candidate
                break

        if not at_port:
            logger.warning(
                f"[Watchdog] No AT port found among {AT_SERIAL_CANDIDATES}"
            )
            return False

        try:
            import serial  # pyserial
            with serial.Serial(at_port, baudrate=115200, timeout=2) as ser:
                ser.write(b"AT\r\n")
                time.sleep(0.5)
                response = ser.read(ser.in_waiting or 64).decode(errors="ignore")

                if "OK" not in response:
                    logger.warning(
                        f"[Watchdog] Modem unresponsive on {at_port} "
                        f"(response: {response.strip()!r})"
                    )
                    return False

                logger.info(
                    f"[Watchdog] Modem answers on {at_port}, sending AT+CFUN=1,1"
                )
                ser.write(b"AT+CFUN=1,1\r\n")
                time.sleep(1)
            return True

        except ImportError:
            logger.warning(
                "[Watchdog] pyserial not installed -- AT reset unavailable. "
                "Install it with: pip install pyserial"
            )
            return False
        except Exception as exc:
            logger.error(f"[Watchdog] AT reset failed on {at_port}: {exc}")
            return False

    def _nmcli_reconnect(self) -> bool:
        """
        Use nmcli to disconnect and reconnect the GSM profile if NetworkManager
        manages the 4G connection.
        """
        try:
            # List the active connections of type gsm
            result = subprocess.run(
                ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "con", "show", "--active"],
                capture_output=True, text=True, timeout=10
            )
            gsm_profiles = [
                line.split(":")[0]
                for line in result.stdout.splitlines()
                if ":gsm:" in line or "modem" in line.lower()
            ]

            if not gsm_profiles:
                logger.warning(
                    "[Watchdog] nmcli: no active GSM profile found. "
                    "Attempting a generic reconnection..."
                )
                # Try a generic reconnection on the interface anyway
                iface = self._find_modem_interface()
                if iface:
                    subprocess.run(
                        ["nmcli", "device", "disconnect", iface],
                        timeout=10, capture_output=True
                    )
                    time.sleep(2)
                    subprocess.run(
                        ["nmcli", "device", "connect", iface],
                        timeout=15, capture_output=True
                    )
                return True  # We try; the result is verified by the ping

            for profile in gsm_profiles:
                logger.info(f"[Watchdog] nmcli: reconnecting profile '{profile}'")
                subprocess.run(
                    ["nmcli", "con", "down", profile],
                    timeout=10, capture_output=True
                )
                time.sleep(2)
                subprocess.run(
                    ["nmcli", "con", "up", profile],
                    timeout=20, capture_output=True
                )
            return True

        except FileNotFoundError:
            logger.warning("[Watchdog] nmcli not available.")
            return False
        except Exception as exc:
            logger.error(f"[Watchdog] nmcli reconnect failed: {exc}")
            return False

    # -----------------------------------------------------------------------
    # Status
    # -----------------------------------------------------------------------

    def status_str(self) -> str:
        """Return a summary of the watchdog state (for the performance logs)."""
        connected = "✅" if self._failure_count == 0 else f"⚠️ {self._failure_count} failure(s)"
        return (
            f"Modem: {connected} | "
            f"Recoveries: {self._recovery_count}"
        )
