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

        self.upload_queue = queue.Queue(maxsize=50)  # Borne pour éviter accumulation infinie
        self.running = True
        self._retry_after = {}  # local_path -> earliest retry timestamp
        self._fail_count = {}   # local_path -> nombre d'échecs consécutifs
        self.MAX_UPLOAD_ATTEMPTS = 20  # Au-delà, déplacé vers failed/ plutôt que retenté à vie
        self.thread: Optional[threading.Thread] = None
        # Consulté par ModemWatchdog avant toute action réseau destructrice
        # (interface down/up, power-cycle USB) pour ne jamais couper un transfert
        # en cours de route.
        self.upload_in_progress = threading.Event()

        # Détection de la méthode d'upload disponible
        # Priorité : rsync (reprend les transferts interrompus) > paramiko SFTP (fallback)
        # rsync nécessite sshpass pour l'authentification par mot de passe.
        self._has_rsync = shutil.which("rsync") is not None
        self._has_sshpass = shutil.which("sshpass") is not None

        if self._has_rsync and self._has_sshpass:
            # Vérifier que rsync est aussi disponible côté NAS distant.
            # Cause la plus fréquente d'erreur code 12 : rsync absent sur le serveur.
            if self._check_remote_rsync():
                print(f"📡 [Upload] Mode rsync activé (reprise sur coupure, bwlimit 500 Ko/s)")
            else:
                print(f"⚠️ [Upload] rsync absent sur le NAS distant → fallback paramiko SFTP")
                print(f"   → Installer rsync sur le NAS pour activer les reprises de transfert.")
                self._has_rsync = False  # Force paramiko définitivement
        elif self._has_rsync and not self._has_sshpass:
            print(f"⚠️ [Upload] rsync présent mais sshpass absent → fallback paramiko SFTP")
            print(f"   → Pour activer rsync : sudo apt-get install -y sshpass")
        else:
            print(f"⚠️ [Upload] rsync absent → mode paramiko SFTP")

        self._start_upload_thread()
        self._rescan_orphaned_logs()

        self.watchdog = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="sftp-watchdog"
        )
        self.watchdog.start()
        print(f"📡 [Upload] Manager démarré (Cible: {self.host}:{self.port})")

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------
    # Remote rsync check
    # -----------------------------------------------------------------------

    def _check_remote_rsync(self):
        """Vérifie si rsync est disponible sur le serveur NAS distant.
        Évite les erreurs code 12 en boucle quand rsync n'est pas installé côté NAS.
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
            print(f"⚠️ [Upload] Impossible de vérifier rsync distant : {e}")
            return False

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def upload_file(self, local_path):
        """Ajoute un fichier à la queue d'upload."""
        if not os.path.exists(local_path):
            print(f"⚠️ [Upload] Fichier introuvable : {local_path}")
            return False
        try:
            self.upload_queue.put_nowait(local_path)
            print(f"📥 [Upload] En queue : {os.path.basename(local_path)}")
            return True
        except queue.Full:
            print(
                f"⚠️ [Upload] Queue pleine ({self.upload_queue.maxsize} fichiers), "
                f"{os.path.basename(local_path)} ignoré pour l'instant."
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
        print(f"🧵 [Upload] Thread démarré.")

    def _watchdog_loop(self):
        """Redémarre le thread d'upload s'il meurt inopinément."""
        while self.running:
            time.sleep(5)
            if self.running and not self.thread.is_alive():
                print(f"⚠️ [Upload] Thread mort ! Redémarrage...")
                self._start_upload_thread()

    def _upload_loop(self):
        while self.running:
            try:
                local_path = self.upload_queue.get(timeout=2.0)
            except queue.Empty:
                continue

            try:
                # Retry non-bloquant : si ce fichier a échoué récemment, on le remet
                # en queue et on passe au suivant plutôt que de bloquer le thread.
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
                    # Fichier disparu avant l'upload (voir _try_rsync_upload/
                    # _try_paramiko_upload) : rien à retenter, on arrête le suivi
                    # sans jamais prétendre que c'était un succès.
                    self._retry_after.pop(local_path, None)
                    self._fail_count.pop(local_path, None)
                elif success:
                    print(f"✅ [Upload] Réussi : {os.path.basename(local_path)}")
                    self._retry_after.pop(local_path, None)
                    self._fail_count.pop(local_path, None)
                    try:
                        os.remove(local_path)
                        print(f"🗑️ [Upload] Fichier local supprimé : {os.path.basename(local_path)}")
                    except Exception as e:
                        print(f"⚠️ [Upload] Impossible de supprimer le fichier local : {e}")
                else:
                    attempts = self._fail_count.get(local_path, 0) + 1
                    self._fail_count[local_path] = attempts
                    if attempts >= self.MAX_UPLOAD_ATTEMPTS:
                        # Trop d'échecs pour ce fichier précis : le retenter pour
                        # toujours occuperait une des 50 places de la queue bornée
                        # sans jamais aboutir (ex: NAS plein, nom de fichier
                        # incompatible). On le sort du cycle actif plutôt que de
                        # bloquer la capacité utile pendant une panne prolongée.
                        self._move_to_failed(local_path)
                        self._retry_after.pop(local_path, None)
                        self._fail_count.pop(local_path, None)
                    else:
                        print(
                            f"❌ [Upload] Échec ({attempts}/{self.MAX_UPLOAD_ATTEMPTS}) : "
                            f"{os.path.basename(local_path)}. Retry dans 60s..."
                        )
                        # Backoff 60s (au lieu de 30s) pour laisser le modem respirer
                        self._retry_after[local_path] = time.time() + 60
                        self._requeue(local_path)

            except Exception as e:
                print(f"🔥 [Upload] Erreur inattendue dans la boucle : {e}")
                self._requeue(local_path)  # Ne pas perdre le fichier si possible
            finally:
                self.upload_queue.task_done()

    def _rescan_orphaned_logs(self):
        """Recharge dans la queue les CSV et fichiers d'alerte de logs/ non
        confirmés uploadés après un redémarrage du process (la queue précédente,
        purement en mémoire, est perdue à chaque redémarrage). Limité à LOG_DIR
        -- jamais VIDEO_DIR : les vidéos peuvent être volontairement conservées
        en local selon le mode d'upload (video+snapshot), on ne devine pas leur
        statut ici. Marge de sécurité (2x ROTATION_MINUTES) pour ne jamais
        tenter de réuploader un fichier encore activement écrit par la session
        en cours.

        Les fichiers ALERTE_*.txt (utils/alerting.py) sont inclus depuis peu :
        avant, seul *.csv était repris, donc une alerte caméra/modem écrite
        juste avant un redémarrage restait bloquée sur la Pi pour toujours --
        le seul canal de visibilité à distance de tout ce système pouvait donc
        rester muet précisément quand une vraie panne se produisait."""
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
                print(f"♻️ [Upload] {recovered} log(s) orphelin(s) repris après redémarrage.")
        except Exception as e:
            print(f"⚠️ [Upload] Rescan des logs orphelins échoué : {e}")

    def _move_to_failed(self, local_path):
        """Sort un fichier du cycle de retry actif après trop d'échecs (voir
        MAX_UPLOAD_ATTEMPTS) : déplacé localement (jamais supprimé, récupérable
        manuellement) plutôt que retenté à vie en occupant une place de la queue."""
        try:
            failed_dir = os.path.join(os.path.dirname(local_path), "failed")
            os.makedirs(failed_dir, exist_ok=True)
            dest = os.path.join(failed_dir, os.path.basename(local_path))
            os.rename(local_path, dest)
            print(
                f"🪦 [Upload] {os.path.basename(local_path)} déplacé vers "
                f"{failed_dir}/ après {self.MAX_UPLOAD_ATTEMPTS} échecs — "
                f"ne sera plus retenté automatiquement."
            )
        except Exception as e:
            print(f"⚠️ [Upload] Impossible de déplacer {local_path} vers failed/ : {e}")

    def _requeue(self, local_path):
        """Remet un fichier en queue sans jamais bloquer ce thread.

        Un put() bloquant ici peut deadlocker : si le producteur (upload_file,
        appelé depuis le thread principal) remplit la queue borné juste après le
        get() de ce thread et avant ce requeue, plus personne ne peut jamais faire
        le get() qui libérerait une place. put_nowait() élimine ce risque — au prix
        d'abandonner le fichier (toujours présent sur disque local, mais plus
        retenté automatiquement) si la queue est déjà pleine."""
        try:
            self.upload_queue.put_nowait(local_path)
        except queue.Full:
            print(
                f"🚨 [Upload] Queue pleine ({self.upload_queue.maxsize}) — "
                f"{os.path.basename(local_path)} abandonné (pas de retry automatique, "
                f"le fichier reste sur le disque local)."
            )
            self._retry_after.pop(local_path, None)

    # -----------------------------------------------------------------------
    # Upload dispatch
    # -----------------------------------------------------------------------

    def _try_upload(self, local_path):
        """Dispatch vers rsync ou paramiko selon disponibilité."""
        if self._has_rsync and self._has_sshpass:
            return self._try_rsync_upload(local_path)
        else:
            return self._try_paramiko_upload(local_path)

    # -----------------------------------------------------------------------
    # Méthode 1 : rsync (prioritaire)
    # -----------------------------------------------------------------------

    def _try_rsync_upload(self, local_path):
        """
        Upload via rsync + sshpass.

        Avantages vs SFTP paramiko :
        - --partial        : conserve le fichier partiellement transféré côté NAS
                             si la connexion est coupée en plein milieu.
        - --append-verify  : reprend le transfert exactement là où il s'était
                             arrêté, puis vérifie la checksum de l'ensemble.
                             → Plus jamais de retransmission depuis le début.
        - --bwlimit=500    : limite à 500 Ko/s pour ne pas saturer le buffer
                             du firmware du modem 4G (évite les freezes).
        - Idempotent       : si le fichier est déjà présent et identique sur
                             le NAS, rsync ne transfère rien (vérification locale).
        """
        if not os.path.exists(local_path):
            print(
                f"🚨 [rsync] Fichier disparu avant l'upload : "
                f"{os.path.basename(local_path)} — abandon, sans vérifier s'il a "
                f"réellement atteint le NAS (ex: supprimé par le nettoyeur de "
                f"disque pendant qu'il attendait en queue)."
            )
            return None  # Ni succès ni échec : rien à retenter, il n'existe plus

        remote_dest = f"{self.username}@{self.host}:{self.remote_dir}/"

        # Options SSH : pas de vérification du host (déploiement embarqué),
        # timeout de connexion rapide pour ne pas bloquer en cas de NAS injoignable.
        ssh_cmd = (
            f"ssh -p {self.port} "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"-o ConnectTimeout=10 "
            f"-o ServerAliveInterval=15 "   # équivalent du keepalive SSH
            f"-o ServerAliveCountMax=2 "    # 2 paquets manqués = connexion coupée
            f"-o BatchMode=yes "            # évite les prompts interactifs et certains banners NAS
            f"-o LogLevel=ERROR"            # supprime les messages info SSH qui corrompent le protocole rsync
        )

        cmd = [
            "sshpass", f"-p{self.password}",
            "rsync",
            "--archive",        # préserve métadonnées (timestamps, permissions)
            "--compress",       # compression en transit (utile pour CSV, neutre pour JPEG)
            "--partial",        # garde le fichier partiel si coupure
            "--append-verify",  # reprend + vérifie checksum
            "--timeout=30",     # timeout réseau rsync (secondes sans activité)
            "--bwlimit=500",    # 500 Ko/s max → protège le buffer du modem
            "--quiet",          # mode silencieux → évite que les messages du NAS corrompent le protocole
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
                2:  "Incompatibilité de protocole",
                5:  "Erreur démarrage client/serveur",
                10: "Erreur I/O socket",
                11: "Erreur accès fichier",
                12: "Erreur protocole de flux de données",
                14: "Erreur IPC (code, message ou flux)",
                20: "Interruption reçue",
                21: "Erreur wait() du processus fils",
                22: "Erreur d'allocation mémoire",
                23: "Transfert partiel (certains fichiers manquants/inaccessibles)",
                24: "Fichiers source disparus en cours de transfert",
                25: "Limite --max-delete atteinte",
                30: "Timeout réseau",
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
                f"⏱️ [rsync] Hard timeout (180s) dépassé pour "
                f"{os.path.basename(local_path)}"
            )
            return False
        except FileNotFoundError:
            # sshpass ou rsync disparu du système
            print(f"❌ [rsync] sshpass/rsync introuvable. Bascule sur paramiko SFTP.")
            self._has_sshpass = False  # Désactive rsync pour les prochains essais
            return self._try_paramiko_upload(local_path)

    # -----------------------------------------------------------------------
    # Méthode 2 : paramiko SFTP (fallback)
    # -----------------------------------------------------------------------

    def _try_paramiko_upload(self, local_path):
        """
        Fallback SFTP via paramiko si rsync/sshpass sont indisponibles.
        Inclut la fermeture propre de la socket TCP (kernel-level)
        pour éviter les connexions zombies dans le firmware du modem.
        """
        import paramiko
        import socket

        transport = None
        sock = None  # Référence explicite pour fermeture propre dans le finally
        try:
            if not os.path.exists(local_path):
                print(
                    f"🚨 [SFTP] Fichier disparu avant l'upload : "
                    f"{os.path.basename(local_path)} — abandon, sans vérifier s'il a "
                    f"réellement atteint le NAS (ex: supprimé par le nettoyeur de "
                    f"disque pendant qu'il attendait en queue)."
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
                print(f"📂 [SFTP] Création du répertoire distant : {self.remote_dir}")
                sftp.mkdir(self.remote_dir)

            remote_path = os.path.join(self.remote_dir, os.path.basename(local_path))
            file_size_mb = os.path.getsize(local_path) / (1024 * 1024)
            print(f"📤 [SFTP] {os.path.basename(local_path)} ({file_size_mb:.1f} MB)")

            upload_exc: list[Optional[Exception]] = [None]

            # Rate-limit paramiko à ~500 Ko/s pour protéger le buffer du modem
            # (même bwlimit que rsync pour éviter les freezes)
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
            # Timeout proportionnel à la taille du fichier et au débit imposé, avec
            # 50% de marge et un plancher de 30s. Un timeout fixe de 30s ne peut
            # mathématiquement jamais suffire au-delà de ~14,6 Mo à 500 Ko/s : tout
            # fichier plus gros échouerait alors à coup sûr, à chaque tentative.
            put_timeout = max(30, int(file_size / BWLIMIT_BYTES_PER_SEC * 1.5) + 10)
            put_thread.join(timeout=put_timeout)

            if put_thread.is_alive():
                # Forcer la fermeture de la socket pour que _do_put lève une exception
                # et s'arrête — sans ça le thread daemon reste bloqué et la connexion
                # TCP reste ouverte côté modem (connexion zombie).
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                raise TimeoutError(
                    f"sftp.put() timeout après {put_timeout}s pour {os.path.basename(local_path)}"
                )
            if upload_exc[0] is not None:
                raise upload_exc[0]

            sftp.close()
            transport.close()
            return True

        except TimeoutError as e:
            # Doit être testé AVANT (socket.gaierror, ..., OSError) : depuis
            # Python 3.10, socket.timeout est un alias de TimeoutError et
            # TimeoutError est une sous-classe d'OSError -- placé après, ce bloc
            # n'était jamais atteint (masqué par "NAS injoignable" ci-dessous,
            # message trompeur pour un simple timeout, ex: celui du point de
            # sftp.put() bloqué).
            print(f"⏱️ [SFTP] Timeout — {e}")
        except (socket.gaierror, ConnectionRefusedError, OSError) as e:
            print(f"🔌 [SFTP] NAS injoignable ({self.host}:{self.port}) — {e}")
        except paramiko.AuthenticationException as e:
            print(f"🔑 [SFTP] Échec authentification ({self.username}@{self.host}) — {e}")
        except Exception as e:
            print(f"⚠️ [SFTP] Erreur upload : {e}")
        finally:
            # Fermeture en cascade : transport → socket brute (kernel)
            # sock.shutdown() + sock.close() garantit que la connexion TCP est
            # terminée au niveau kernel même si le transport n'a pas pu s'en charger.
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
        """Arrête le manager.

        Avant, cette méthode se contentait de couper un flag : le thread
        d'upload est daemon=True, donc tué instantanément à la sortie du
        process, qu'il ait fini ou non. Sur CHAQUE arrêt (SIGTERM, redémarrage
        planifié, sys.exit(1) après erreur fatale), les fichiers enfilés juste
        avant -- notamment les CSV de la dernière session, et pire, une alerte
        caméra/modem critique -- pouvaient être silencieusement abandonnés en
        mémoire. On attend maintenant, borné à drain_timeout, que la file soit
        vide ET qu'aucun transfert ne soit en cours avant de rendre la main.
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
                    f"⚠️ [Upload] Arrêt après {drain_timeout:.0f}s d'attente : "
                    f"{remaining} fichier(s) encore en file, abandonnés (non uploadés)."
                )
        self.running = False
        print("🛑 [Upload] Arrêt du manager...")
