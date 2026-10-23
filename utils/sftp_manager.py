import threading
import os
import glob
import subprocess
import shutil
import queue
import time
from typing import Optional
from utils.configs import (
    SFTP_HOST, SFTP_PORT, SFTP_USERNAME, SFTP_PASSWORD, SFTP_REMOTE_DIR,
    LOG_DIR, ROTATION_MINUTES,
)


class SFTPManager:
    def __init__(self):
        self.host = SFTP_HOST
        self.port = SFTP_PORT
        self.username = SFTP_USERNAME
        self.password = SFTP_PASSWORD
        self.remote_dir = SFTP_REMOTE_DIR

        # Les identifiants sont vides dans le depot et renseignes par
        # setup_agent.sh. S'ils le sont restes, aucun transfert n'est possible :
        # sans ce message l'agent tournait normalement et accumulait tout sur la
        # carte SD, sans que rien ne signale que le NAS ne recevait rien.
        self._configured = bool(self.host and self.username)
        if not self._configured:
            print(
                "\u26a0\ufe0f [Upload] Identifiants NAS absents (SFTP_HOST/SFTP_USERNAME "
                "vides dans utils/configs.py) : aucun transfert ne sera tente, les "
                "fichiers resteront sur la carte SD. Relancer setup_agent.sh ou "
                "renseigner utils/configs.py."
            )

        self.upload_queue = queue.Queue(maxsize=50)  # Bounded, to avoid unbounded accumulation
        self.running = True
        self._retry_after = {}  # local_path -> earliest retry timestamp
        self._fail_count = {}   # local_path -> number of consecutive failures
        self.MAX_UPLOAD_ATTEMPTS = 20  # Beyond that, moved to failed/ rather than retried forever
        self.thread: Optional[threading.Thread] = None
        # Read by ModemWatchdog before any destructive network action
        # (interface down/up, USB power cycle) so that a transfer in flight is
        # never cut off.
        self.upload_in_progress = threading.Event()

        # Detect the available upload method.
        # Priority: rsync (resumes interrupted transfers) > paramiko SFTP (fallback).
        # rsync needs sshpass for password authentication.
        self._has_rsync = shutil.which("rsync") is not None
        self._has_sshpass = shutil.which("sshpass") is not None

        if self._has_rsync and self._has_sshpass:
            # Check that rsync is also available on the remote NAS.
            # Most frequent cause of error code 12: rsync missing on the server.
            if self._check_remote_rsync():
                print(f"📡 [Upload] rsync mode enabled (resume on interruption, bwlimit 500 kB/s)")
            else:
                print(f"⚠️ [Upload] rsync missing on the remote NAS -> paramiko SFTP fallback")
                print(f"   -> Install rsync on the NAS to enable resumable transfers.")
                self._has_rsync = False  # Force paramiko for good
        elif self._has_rsync and not self._has_sshpass:
            print(f"⚠️ [Upload] rsync present but sshpass missing -> paramiko SFTP fallback")
            print(f"   -> To enable rsync: sudo apt-get install -y sshpass")
        else:
            print(f"⚠️ [Upload] rsync missing -> paramiko SFTP mode")

        self._start_upload_thread()
        self._rescan_orphaned_logs()

        self.watchdog = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="sftp-watchdog"
        )
        self.watchdog.start()
        print(f"📡 [Upload] Manager started (target: {self.host}:{self.port})")

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------
    # Remote rsync check
    # -----------------------------------------------------------------------

    def _check_remote_rsync(self):
        """Check whether rsync is available on the remote NAS server.
        Avoids repeated error-12 failures when rsync is not installed on the NAS.
        """
        try:
            cmd = [
                "sshpass", f"-p{self.password}",
                "ssh",
                "-p", str(self.port),
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ConnectTimeout=10",
                "-o", "BatchMode=yes",
                "-o", "LogLevel=ERROR",
                f"{self.username}@{self.host}",
                "which rsync"
            ]
            result = subprocess.run(cmd, timeout=15, capture_output=True, text=True)
            return result.returncode == 0
        except Exception as e:
            print(f"⚠️ [Upload] Could not check for remote rsync: {e}")
            return False

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def upload_file(self, local_path):
        """Add a file to the upload queue."""
        if not os.path.exists(local_path):
            print(f"⚠️ [Upload] File not found: {local_path}")
            return False
        try:
            self.upload_queue.put_nowait(local_path)
            print(f"📥 [Upload] Queued: {os.path.basename(local_path)}")
            return True
        except queue.Full:
            print(
                f"⚠️ [Upload] Queue full ({self.upload_queue.maxsize} files), "
                f"{os.path.basename(local_path)} skipped for now."
            )
            return False

    # -----------------------------------------------------------------------
    # Thread management
    # -----------------------------------------------------------------------

    def _start_upload_thread(self):
        self.thread = threading.Thread(
            target=self._upload_loop, daemon=True, name="sftp-upload"
        )
        self.thread.start()
        print(f"🧵 [Upload] Thread started.")

    def _watchdog_loop(self):
        """Restart the upload thread if it dies unexpectedly."""
        while self.running:
            time.sleep(5)
            if self.running and not self.thread.is_alive():
                print(f"⚠️ [Upload] Thread died! Restarting...")
                self._start_upload_thread()

    def _upload_loop(self):
        while self.running:
            try:
                local_path = self.upload_queue.get(timeout=2.0)
            except queue.Empty:
                continue

            try:
                # Non-blocking retry: if this file failed recently, it is put back
                # in the queue and we move on rather than blocking the thread.
                retry_at = self._retry_after.get(local_path, 0)
                if time.time() < retry_at:
                    self._requeue(local_path)
                    time.sleep(1.0)
                    continue

                self.upload_in_progress.set()
                try:
                    success = self._try_upload(local_path)
                finally:
                    self.upload_in_progress.clear()

                if success is None:
                    # File vanished before the upload (see _try_rsync_upload /
                    # _try_paramiko_upload): nothing to retry, we stop tracking it
                    # without ever pretending it was a success.
                    self._retry_after.pop(local_path, None)
                    self._fail_count.pop(local_path, None)
                elif success:
                    print(f"✅ [Upload] Succeeded: {os.path.basename(local_path)}")
                    self._retry_after.pop(local_path, None)
                    self._fail_count.pop(local_path, None)
                    try:
                        os.remove(local_path)
                        print(f"🗑️ [Upload] Local file deleted: {os.path.basename(local_path)}")
                    except Exception as e:
                        print(f"⚠️ [Upload] Could not delete the local file: {e}")
                else:
                    attempts = self._fail_count.get(local_path, 0) + 1
                    self._fail_count[local_path] = attempts
                    if attempts >= self.MAX_UPLOAD_ATTEMPTS:
                        # Too many failures for this particular file: retrying it
                        # forever would occupy one of the 50 slots of the bounded
                        # queue without ever succeeding (e.g. NAS full, incompatible
                        # file name). We take it out of the active cycle rather than
                        # blocking useful capacity during a prolonged outage.
                        self._move_to_failed(local_path)
                        self._retry_after.pop(local_path, None)
                        self._fail_count.pop(local_path, None)
                    else:
                        print(
                            f"❌ [Upload] Failed ({attempts}/{self.MAX_UPLOAD_ATTEMPTS}): "
                            f"{os.path.basename(local_path)}. Retry in 60s..."
                        )
                        # 60s backoff (instead of 30s) to let the modem breathe
                        self._retry_after[local_path] = time.time() + 60
                        self._requeue(local_path)

            except Exception as e:
                print(f"🔥 [Upload] Unexpected error in the loop: {e}")
                self._requeue(local_path)  # Do not lose the file if we can help it
            finally:
                self.upload_queue.task_done()

    def _rescan_orphaned_logs(self):
        """Put back in the queue the CSV and alert files of logs/ that were not
        confirmed as uploaded before a process restart (the previous queue, held
        purely in memory, is lost on every restart). Limited to LOG_DIR -- never
        VIDEO_DIR: videos may be deliberately kept locally depending on the
        upload mode (video+snapshot), and their status is not guessed here. A
        safety margin (2x ROTATION_MINUTES) makes sure a file still being written
        by the current session is never re-uploaded.

        ALERTE_*.txt files (utils/alerting.py) have recently been included:
        before, only *.csv was recovered, so a camera or modem alert written just
        before a restart stayed stuck on the Pi forever -- the only remote
        visibility channel of this whole system could therefore stay silent
        precisely when a real fault occurred."""
        try:
            safety_margin_s = ROTATION_MINUTES * 60 * 2
            now = time.time()
            recovered = 0
            patterns = [os.path.join(LOG_DIR, "*.csv"), os.path.join(LOG_DIR, "ALERTE_*.txt")]
            for pattern in patterns:
                for path in glob.glob(pattern):
                    try:
                        if now - os.path.getmtime(path) > safety_margin_s:
                            if self.upload_file(path):
                                recovered += 1
                    except OSError:
                        continue
            if recovered:
                print(f"♻️ [Upload] {recovered} orphaned log(s) picked up after restart.")
        except Exception as e:
            print(f"⚠️ [Upload] Rescan of orphaned logs failed: {e}")

    def _move_to_failed(self, local_path):
        """Take a file out of the active retry cycle after too many failures (see
        MAX_UPLOAD_ATTEMPTS): moved locally (never deleted, recoverable by hand)
        rather than retried forever while occupying a queue slot."""
        try:
            failed_dir = os.path.join(os.path.dirname(local_path), "failed")
            os.makedirs(failed_dir, exist_ok=True)
            dest = os.path.join(failed_dir, os.path.basename(local_path))
            os.rename(local_path, dest)
            print(
                f"🪦 [Upload] {os.path.basename(local_path)} moved to "
                f"{failed_dir}/ after {self.MAX_UPLOAD_ATTEMPTS} failures -- "
                f"it will no longer be retried automatically."
            )
        except Exception as e:
            print(f"⚠️ [Upload] Could not move {local_path} to failed/: {e}")

    def _requeue(self, local_path):
        """Put a file back in the queue without ever blocking this thread.

        A blocking put() here can deadlock: if the producer (upload_file, called
        from the main thread) fills the bounded queue right after this thread's
        get() and before this requeue, nobody can ever perform the get() that
        would free a slot. put_nowait() removes that risk -- at the cost of
        abandoning the file (still present on local disk, but no longer retried
        automatically) if the queue is already full."""
        try:
            self.upload_queue.put_nowait(local_path)
        except queue.Full:
            print(
                f"🚨 [Upload] Queue pleine ({self.upload_queue.maxsize}) — "
                f"{os.path.basename(local_path)} abandoned (no automatic retry, "
                f"the file stays on the local disk)."
            )
            self._retry_after.pop(local_path, None)

    # -----------------------------------------------------------------------
    # Upload dispatch
    # -----------------------------------------------------------------------

    def _try_upload(self, local_path):
        """Dispatch to rsync or paramiko depending on availability."""
        if self._has_rsync and self._has_sshpass:
            return self._try_rsync_upload(local_path)
        else:
            return self._try_paramiko_upload(local_path)

    # -----------------------------------------------------------------------
    # Method 1: rsync (preferred)
    # -----------------------------------------------------------------------

    def _try_rsync_upload(self, local_path):
        """
        Upload via rsync + sshpass.

        Advantages over paramiko SFTP:
        - --partial        : keeps the partially transferred file on the NAS side
                             if the connection is cut in the middle.
        - --append-verify  : resumes the transfer exactly where it stopped, then
                             verifies the checksum of the whole file.
                             -> No more retransmission from the start.
        - --bwlimit=500    : caps at 500 kB/s so as not to saturate the buffer of
                             the 4G modem firmware (avoids freezes).
        - Idempotent       : if the file is already present and identical on the
                             NAS, rsync transfers nothing (local check).
        """
        if not os.path.exists(local_path):
            print(
                f"🚨 [rsync] Fichier disparu avant l'upload : "
                f"{os.path.basename(local_path)} -- giving up, without checking "
                f"whether it actually reached the NAS (e.g. deleted by the disk "
                f"cleaner while it was waiting in the queue)."
            )
            return None  # Neither success nor failure: nothing to retry, it is gone

        remote_dest = f"{self.username}@{self.host}:{self.remote_dir}/"

        # SSH options: no host verification (embedded deployment), short connect
        # timeout so we do not hang when the NAS is unreachable.
        ssh_cmd = (
            f"ssh -p {self.port} "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=10 "
            f"-o ServerAliveInterval=15 "   # SSH keepalive equivalent
            f"-o ServerAliveCountMax=2 "    # 2 missed packets = connection dropped
            f"-o BatchMode=yes "            # avoids interactive prompts and some NAS banners
            f"-o LogLevel=ERROR"            # supprime les messages info SSH qui corrompent le protocole rsync
        )

        cmd = [
            "sshpass", f"-p{self.password}",
            "rsync",
            "--archive",        # preserves metadata (timestamps, permissions)
            "--compress",       # compression en transit (utile pour CSV, neutre pour JPEG)
            "--partial",        # garde le fichier partiel si coupure
            "--append-verify",  # resumes + verifies the checksum
            "--timeout=30",     # rsync network timeout (seconds without activity)
            "--bwlimit=500",    # 500 kB/s max -> protects the modem buffer
            "--quiet",          # quiet mode -> keeps NAS messages from corrupting the protocol
            "-e", ssh_cmd,
            local_path,
            remote_dest,
        ]

        file_size_mb = os.path.getsize(local_path) / (1024 * 1024)
        print(
            f"📤 [rsync] {os.path.basename(local_path)} "
            f"({file_size_mb:.1f} MB) → {self.host}"
        )

        try:
            result = subprocess.run(
                cmd,
                timeout=180,        # hard timeout process : 3 min max par fichier
                capture_output=True,
                text=True,
            )

            if result.returncode == 0:
                return True

            # Codes d'erreur rsync utiles pour le diagnostic
            rsync_error_codes = {
                1:  "Erreur de syntaxe / usage",
                2:  "Protocol incompatibility",
                5:  "Client/server start-up error",
                10: "Erreur I/O socket",
                11: "File access error",
                12: "Data stream protocol error",
                14: "Erreur IPC (code, message ou flux)",
                20: "Interrupt received",
                21: "Erreur wait() du processus fils",
                22: "Memory allocation error",
                23: "Transfert partiel (certains fichiers manquants/inaccessibles)",
                24: "Fichiers source disparus en cours de transfert",
                25: "Limite --max-delete atteinte",
                30: "Network timeout",
                35: "Timeout de connexion",
            }
            err_label = rsync_error_codes.get(result.returncode, f"Code {result.returncode}")
            stderr_tail = result.stderr.strip().split('\n')[-1] if result.stderr.strip() else ""
            print(f"❌ [rsync] Échec — {err_label} : {stderr_tail}")

            # Erreur 12 = protocole corrompu (souvent banner/MOTD du NAS) → fallback paramiko
            if result.returncode == 12:
                print(f"⚠️ [rsync] Erreur protocole (code 12) — Bascule sur paramiko SFTP pour ce fichier.")
                return self._try_paramiko_upload(local_path)

            return False

        except subprocess.TimeoutExpired:
            print(
                f"⏱️ [rsync] Hard timeout (180s) exceeded for "
                f"{os.path.basename(local_path)}"
            )
            return False
        except FileNotFoundError:
            # sshpass or rsync vanished from the system
            print(f"❌ [rsync] sshpass/rsync introuvable. Bascule sur paramiko SFTP.")
            self._has_sshpass = False  # Disable rsync for subsequent attempts
            return self._try_paramiko_upload(local_path)

    # -----------------------------------------------------------------------
    # Method 2: paramiko SFTP (fallback)
    # -----------------------------------------------------------------------

    def _try_paramiko_upload(self, local_path):
        """
        Fallback SFTP via paramiko si rsync/sshpass sont indisponibles.
        Inclut la fermeture propre de la socket TCP (kernel-level)
        to avoid zombie connections inside the modem firmware.
        """
        import paramiko
        import socket

        transport = None
        sock = None  # Explicit reference, for a clean close in the finally block
        try:
            if not os.path.exists(local_path):
                print(
                    f"🚨 [SFTP] Fichier disparu avant l'upload : "
                    f"{os.path.basename(local_path)} -- giving up, without checking "
                    f"whether it actually reached the NAS (e.g. deleted by the disk "
                    f"cleaner while it was waiting in the queue)."
                )
                return None

            sock = socket.create_connection((self.host, self.port), timeout=10)
            sock.settimeout(30)
            transport = paramiko.Transport(
                sock, default_window_size=8192, default_max_packet_size=900
            )
            transport.set_keepalive(15)
            transport.connect(username=self.username, password=self.password)
            sftp = paramiko.SFTPClient.from_transport(transport)

            try:
                sftp.stat(self.remote_dir)
            except FileNotFoundError:
                print(f"📂 [SFTP] Creating the remote directory: {self.remote_dir}")
                sftp.mkdir(self.remote_dir)

            remote_path = os.path.join(self.remote_dir, os.path.basename(local_path))
            file_size_mb = os.path.getsize(local_path) / (1024 * 1024)
            print(f"📤 [SFTP] {os.path.basename(local_path)} ({file_size_mb:.1f} MB)")

            upload_exc: list[Optional[Exception]] = [None]

            # Rate-limit paramiko to ~500 kB/s to protect the modem buffer
            # (same bwlimit as rsync, to avoid freezes)
            BWLIMIT_BYTES_PER_SEC = 500 * 1024  # 500 Ko/s
            file_size = os.path.getsize(local_path)

            def _do_put():
                try:
                    CHUNK = 32768  # 32 Ko par bloc
                    sent = 0
                    t_start = time.time()
                    with open(local_path, "rb") as f_local:
                        with sftp.open(remote_path, "wb") as f_remote:
                            while True:
                                data = f_local.read(CHUNK)
                                if not data:
                                    break
                                f_remote.write(data)
                                sent += len(data)
                                # Throttle
                                elapsed = time.time() - t_start
                                expected = sent / BWLIMIT_BYTES_PER_SEC
                                if expected > elapsed:
                                    time.sleep(expected - elapsed)
                except Exception as e:
                    upload_exc[0] = e

            put_thread = threading.Thread(target=_do_put, daemon=True)
            put_thread.start()
            # Timeout proportional to the file size and to the imposed rate, with
            # 50 % margin and a 30s floor. A fixed 30s timeout can mathematically
            # never be enough beyond ~14.6 MB at 500 kB/s: any larger file would
            # then fail for certain, on every attempt.
            put_timeout = max(30, int(file_size / BWLIMIT_BYTES_PER_SEC * 1.5) + 10)
            put_thread.join(timeout=put_timeout)

            if put_thread.is_alive():
                # Force the socket closed so that _do_put raises an exception and
                # stops -- without this the daemon thread stays blocked and the TCP
                # connection stays open on the modem side (zombie connection).
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                raise TimeoutError(
                    f"sftp.put() timed out after {put_timeout}s for {os.path.basename(local_path)}"
                )
            if upload_exc[0] is not None:
                raise upload_exc[0]

            sftp.close()
            transport.close()
            return True

        except TimeoutError as e:
            # Must be tested BEFORE (socket.gaierror, ..., OSError): since
            # Python 3.10, socket.timeout is an alias of TimeoutError and
            # TimeoutError is a subclass of OSError -- placed after, this block
            # was never reached (masked by "NAS unreachable" below, a misleading
            # message for a plain timeout, e.g. that of a stuck sftp.put()).
            print(f"⏱️ [SFTP] Timeout — {e}")
        except (socket.gaierror, ConnectionRefusedError, OSError) as e:
            print(f"🔌 [SFTP] NAS unreachable ({self.host}:{self.port}) -- {e}")
        except paramiko.AuthenticationException as e:
            print(f"🔑 [SFTP] Authentication failed ({self.username}@{self.host}) -- {e}")
        except Exception as e:
            print(f"⚠️ [SFTP] Upload error: {e}")
        finally:
            # Cascading close: transport -> raw socket (kernel).
            # sock.shutdown() + sock.close() guarantees the TCP connection is
            # terminated at kernel level even if the transport could not do it.
            if transport:
                try:
                    transport.close()
                except Exception:
                    pass
            if sock:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    sock.close()
                except Exception:
                    pass
        return False

    # -----------------------------------------------------------------------

    def stop(self, drain_timeout: float = 30.0):
        """Stop the manager.

        Before, this method only cleared a flag: the upload thread is
        daemon=True, hence killed instantly when the process exits, whether it
        had finished or not. On EVERY shutdown (SIGTERM, planned restart,
        sys.exit(1) after a fatal error), the files queued just before -- notably
        the CSV files of the last session, and worse, a critical camera or modem
        alert -- could be silently abandoned in memory. We now wait, bounded by
        drain_timeout, for the queue to be empty AND for no transfer to be in
        progress before returning.
        """
        deadline = time.monotonic() + drain_timeout
        while time.monotonic() < deadline:
            if self.upload_queue.empty() and not self.upload_in_progress.is_set():
                break
            time.sleep(0.5)
        else:
            remaining = self.upload_queue.qsize()
            if remaining or self.upload_in_progress.is_set():
                print(
                    f"⚠️ [Upload] Stopping after {drain_timeout:.0f}s of waiting: "
                    f"{remaining} file(s) still queued, abandoned (not uploaded)."
                )
        self.running = False
        print("🛑 [Upload] Stopping the manager...")
