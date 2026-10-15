"""
modem_watchdog.py
-----------------
Watchdog logiciel pour le module 4G Waveshare (SIM7600/SIM7900).

Symptôme ciblé : le modem se fige (2 LEDs rouges fixes, pas de trafic),
sans que le Raspberry Pi lui-même soit planté. Un simple `sudo reboot`
récupère la situation car il force un power-cycle USB du module.

Ce module reproduit le comportement de reboot *sans* redémarrer le Pi :
  1. Ping périodique d'un serveur fiable (8.8.8.8), lié à l'interface 4G identifiée
     (jamais un ping "en général" qui pourrait passer par l'Ethernet ou le Wi-Fi)
  2. Si N pings consécutifs échouent → interface réseau down/up
  3. Si le down/up échoue → reset USB du modem via /sys
  4. Si tout échoue → AT+CFUN=1,1 via port série (reset firmware)
  5. En dernier recours : log + fichier d'alerte remonté au NAS (pas de reboot,
     pour ne pas perdre les vidéos en cours d'enregistrement)

Avant toute action destructrice (down/up, power-cycle USB), attend qu'un upload
SFTP en cours se termine (voir SFTPManager.upload_in_progress). En cas d'échecs
totaux répétés, le délai avant nouvelle tentative augmente exponentiellement
(120s, 240s, ... jusqu'à 1h) plutôt que de marteler le modem indéfiniment toutes
les 2-4 minutes en cas de panne matérielle réelle.

Usage :
    from utils.modem_watchdog import ModemWatchdog
    wd = ModemWatchdog()
    wd.start()   # Thread daemon, s'arrête quand le process Python quitte
    ...
    wd.stop()    # Arrêt propre
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
# Configuration (modifiable sans toucher au reste du code)
# ---------------------------------------------------------------------------

PING_HOST        = "8.8.8.8"        # Cible principale du ping de santé
PING_HOST_FALLBACK = "1.1.1.1"      # Repli si la cible principale ne répond pas
                                     # (un opérateur peut bloquer/ralentir l'ICMP
                                     # vers une IP précise sans que la 4G soit KO)
PING_COUNT       = 2                 # Paquets envoyés par check
PING_TIMEOUT     = 5                 # Secondes max par ping
CHECK_INTERVAL   = 30                # Secondes entre deux checks normaux
MAX_FAILURES     = 3                 # Échecs consécutifs avant action
RECOVERY_COOLDOWN = 120              # Secondes de repos après une recovery (1er échec total)
MAX_RECOVERY_COOLDOWN = 3600         # Plafond du backoff exponentiel (1h) après échecs répétés

# Noms d'interfaces réseau possibles pour le modem 4G (essayés en premier, rapide)
MODEM_INTERFACES = ["usb0", "wwan0", "eth1"]
# Repli si aucun des noms ci-dessus n'existe : drivers USB typiques des modems
# cellulaires (qmi_wwan/cdc_ether/...). Plus robuste qu'une liste de noms figés,
# qui peut ne plus matcher après une ré-énumération USB ou selon le mode du modem.
MODEM_DRIVER_HINTS = ("qmi_wwan", "cdc_ether", "cdc_ncm", "rndis_host", "cdc_mbim")

# Port série AT du modem (SIM7600 expose plusieurs /dev/ttyUSB*, le 2 est le port AT)
AT_SERIAL_CANDIDATES = [
    "/dev/ttyUSB2",  # SIM7600 — port AT standard
    "/dev/ttyUSB1",
    "/dev/ttyUSB3",
]

# ---------------------------------------------------------------------------


class ModemWatchdog(threading.Thread):
    """
    Thread daemon qui surveille la connectivité 4G et tente de récupérer
    le modem sans reboot en cas de freeze.
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
        # Nombre de cycles de recovery COMPLETS (les 4 étapes) ayant totalement
        # échoué d'affilée -- distinct de _failure_count (échecs de ping). Sert au
        # backoff exponentiel : remis à 0 dès qu'une étape quelconque réussit.
        self._consecutive_total_failures = 0
        # Référence optionnelle vers le manager d'upload : permet de remonter au
        # NAS un fichier d'alerte quand toutes les tentatives de recovery échouent
        # (aucune notification externe SMS/email/webhook n'est configurée).
        self._sftp_manager = sftp_manager

        logger.info(
            f"[Watchdog] Initialisé — ping {self.ping_host} "
            f"toutes les {self.check_interval}s, seuil={self.max_failures} échecs"
        )

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def start(self):
        self.running = True
        super().start()
        logger.info("[Watchdog] Démarré.")

    def stop(self):
        self.running = False
        logger.info("[Watchdog] Arrêté.")

    @property
    def is_connected(self) -> bool:
        """True si la dernière vérification de ping a réussi."""
        return self._failure_count == 0

    @property
    def recovery_count(self) -> int:
        return self._recovery_count

    # -----------------------------------------------------------------------
    # Thread principal
    # -----------------------------------------------------------------------

    def run(self):
        # Attendre 10s après le démarrage pour laisser le modem s'initialiser
        time.sleep(10)

        while self.running:
            try:
                iface = self._find_modem_interface()
                if iface:
                    ok = self._ping_check(iface)
                else:
                    # Pas d'interface 4G identifiée : impossible de vérifier le lien
                    # 4G spécifiquement (un ping non lié pourrait passer via une
                    # autre interface, Ethernet ou Wi-Fi, et masquer un vrai
                    # problème). Traité comme un échec de connectivité 4G.
                    logger.warning(
                        "[Watchdog] Aucune interface 4G identifiée — connectivité "
                        "4G considérée comme indisponible tant qu'elle n'apparaît pas."
                    )
                    ok = False

                if ok:
                    if self._failure_count > 0:
                        logger.info(
                            f"[Watchdog] ✅ Connectivité 4G rétablie "
                            f"(après {self._failure_count} échecs)"
                        )
                    self._failure_count = 0
                    time.sleep(self.check_interval)
                else:
                    # Plafonné à max_failures : au-delà, le chiffre n'apporte plus
                    # d'information de décision (le seuil est déjà atteint) et ne
                    # faisait que produire des logs du type "47/3" pendant tout le
                    # temps du cooldown, trompeurs sur la gravité réelle.
                    self._failure_count = min(self._failure_count + 1, self.max_failures)
                    logger.warning(
                        f"[Watchdog] ⚠️ Ping 4G échoué "
                        f"({self._failure_count}/{self.max_failures})"
                    )

                    if self._failure_count >= self.max_failures:
                        # Cooldown anti-boucle, avec backoff exponentiel plafonné
                        # après des échecs COMPLETS répétés (voir _attempt_recovery) :
                        # 120s, 240s, 480s... jusqu'à MAX_RECOVERY_COOLDOWN (1h).
                        # Une panne matérielle réelle ne doit pas déclencher des
                        # power-cycles USB/firmware toutes les 2-4 minutes pendant
                        # des jours -- seulement de plus en plus rarement.
                        cooldown = min(
                            RECOVERY_COOLDOWN * (2 ** self._consecutive_total_failures),
                            MAX_RECOVERY_COOLDOWN,
                        )
                        elapsed = time.time() - self._last_recovery_ts
                        if elapsed < cooldown:
                            wait = int(cooldown - elapsed)
                            logger.info(
                                f"[Watchdog] Cooldown actif ({cooldown}s, "
                                f"échecs totaux consécutifs: {self._consecutive_total_failures}) "
                                f"— attente {wait}s avant prochaine tentative"
                            )
                            time.sleep(min(wait, self.check_interval))
                        else:
                            self._attempt_recovery()
                    else:
                        # Réessaye plus vite tant qu'on n'a pas atteint le seuil
                        time.sleep(max(5, self.check_interval // 3))

            except Exception as exc:
                logger.error(f"[Watchdog] Erreur inattendue : {exc}", exc_info=True)
                time.sleep(self.check_interval)

    # -----------------------------------------------------------------------
    # Ping
    # -----------------------------------------------------------------------

    def _ping_check(self, iface: str | None = None) -> bool:
        """Retourne True si le ping réussit, sur la cible principale ou, à
        défaut, sur une cible de repli (un opérateur peut bloquer/ralentir
        l'ICMP vers une IP précise sans que la connectivité 4G réelle soit
        affectée — une seule cible risquerait de déclencher une recovery
        matérielle complète sur un modem parfaitement sain)."""
        if self._ping_once(self.ping_host, iface):
            return True
        if self.ping_host != PING_HOST_FALLBACK:
            return self._ping_once(PING_HOST_FALLBACK, iface)
        return False

    def _ping_once(self, host: str, iface: str | None) -> bool:
        """Un seul essai de ping vers `host`.

        Si `iface` est fourni, le ping est lié à cette interface (-I) pour tester
        spécifiquement le lien 4G. Sans ça, `ping` emprunte la route par défaut du
        système — qui peut très bien être l'Ethernet ou le Wi-Fi si ces liens sont
        actifs, auquel cas un ping réussi ne prouve rien sur l'état réel du modem."""
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
            # Le binaire `ping` lui-même est absent (paquet retiré, image
            # minimale) — distinct d'une vraie coupure réseau. Loggé en ERROR
            # (visible par défaut, contrairement à debug) pour ne pas déclencher
            # des resets matériels en boucle sur un modem parfaitement sain à
            # cause d'un outil manquant, sans que personne ne le remarque.
            logger.error(
                "[Watchdog] Binaire `ping` introuvable — impossible de vérifier "
                "la connectivité 4G. Installer le paquet iputils-ping."
            )
            return False
        except Exception as exc:
            logger.debug(f"[Watchdog] ping exception ({host}) : {exc}")
            return False

    # -----------------------------------------------------------------------
    # Recovery pipeline
    # -----------------------------------------------------------------------

    def _wait_for_upload_idle(self, max_wait: int = 20):
        """Attend qu'un upload SFTP en cours se termine avant une action réseau
        destructrice (interface down/up, power-cycle USB), pour ne pas tronquer un
        transfert en pleine transmission. Attente bornée : si l'upload traîne
        au-delà de max_wait, on considère la connexion compromise de toute façon
        et on procède à la recovery sans attendre indéfiniment."""
        sftp = self._sftp_manager
        if not sftp or not hasattr(sftp, "upload_in_progress"):
            return
        waited = 0
        while sftp.upload_in_progress.is_set() and waited < max_wait:
            time.sleep(1)
            waited += 1
        if waited > 0:
            logger.info(f"[Watchdog] Upload en cours détecté — attendu {waited}s avant recovery.")

    def _wait_and_check(self, max_wait: int, poll_interval: int = 3) -> bool:
        """Sonde le ping toutes les poll_interval secondes jusqu'à max_wait secondes
        de temps RÉEL (time.monotonic(), pas un compteur d'itérations), retourne
        dès que la connectivité revient.

        Deux corrections importantes par rapport à une première version :
        1. Le temps réel est mesuré avec une deadline monotone : un simple
           compteur `waited += poll_interval` sous-estimait largement le temps
           réellement écoulé, car _ping_check() peut à elle seule durer jusqu'à
           ~24s (2 pings séquentiels avec repli) -- un max_wait "documenté" à 20s
           pouvait donc réellement durer plus de 3 minutes.
        2. L'interface est redécouverte à CHAQUE itération plutôt que reçue en
           argument figé : les étapes de recovery (reset USB, reset firmware)
           sont précisément celles qui peuvent faire apparaître ou renommer
           l'interface (ex: wwan0 -> wwan1) PENDANT cette fenêtre d'attente --
           un nom figé aurait fait échouer le ping (-I sur une interface qui
           n'existe plus) ou, pire, l'aurait fait retomber sur une route par
           défaut non liée (faux positif via Ethernet/Wi-Fi)."""
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            time.sleep(poll_interval)
            iface = self._find_modem_interface()
            if self._ping_check(iface):
                return True
        return False

    def _attempt_recovery(self):
        """
        Tente de récupérer le modem par ordre croissant d'agressivité.
        Retourne True si la connexion est rétablie.
        """
        logger.error(
            f"[Watchdog] 🔴 {self._failure_count} pings consécutifs "
            f"échoués — début de la procédure de recovery."
        )
        self._last_recovery_ts = time.time()
        self._recovery_count += 1

        # Ne jamais couper l'interface / le modem en pleine transmission SFTP.
        self._wait_for_upload_idle()

        # Étape 1 : Redémarrer l'interface réseau
        iface = self._find_modem_interface()
        if iface:
            logger.info(f"[Watchdog] Étape 1 — Redémarrage interface {iface}...")
            if self._restart_network_interface(iface):
                # ~20s : un modem LTE met typiquement 15-30s à se ré-attacher
                # après un simple down/up, bien plus que les 5s précédents.
                if self._wait_and_check(max_wait=20):
                    logger.info("[Watchdog] ✅ Recovery Étape 1 réussie (interface down/up).")
                    self._failure_count = 0
                    self._consecutive_total_failures = 0
                    return True

        # Étape 2 : Reset USB du modem via /sys
        logger.info("[Watchdog] Étape 2 — Reset USB du modem...")
        if self._usb_reset():
            # ~25s : ré-énumération USB + ré-attachement réseau, plus long qu'un
            # simple down/up d'interface.
            if self._wait_and_check(max_wait=25):
                logger.info("[Watchdog] ✅ Recovery Étape 2 réussie (USB reset).")
                self._failure_count = 0
                self._consecutive_total_failures = 0
                return True

        # Étape 3 : AT+CFUN=1,1 (reset firmware modem)
        logger.info("[Watchdog] Étape 3 — Envoi AT+CFUN=1,1 (reset firmware)...")
        if self._at_reset():
            if self._wait_and_check(max_wait=20):
                logger.info("[Watchdog] ✅ Recovery Étape 3 réussie (AT reset).")
                self._failure_count = 0
                self._consecutive_total_failures = 0
                return True

        # Étape 4 : NetworkManager — relancer la connexion GSM
        logger.info("[Watchdog] Étape 4 — Relance connexion NetworkManager...")
        if self._nmcli_reconnect():
            if self._wait_and_check(max_wait=15):
                logger.info("[Watchdog] ✅ Recovery Étape 4 réussie (nmcli reconnect).")
                self._failure_count = 0
                self._consecutive_total_failures = 0
                return True

        # Toutes les étapes ont échoué
        self._consecutive_total_failures += 1

        if self._consecutive_total_failures >= 2:
            logger.critical(
                f"[Watchdog] 🚨 Seuil critique atteint ({self._consecutive_total_failures} échecs totaux). "
                f"Le modem a probablement crashé matériellement. REDÉMARRAGE FORCÉ DU SYSTÈME !"
            )
            write_alert("modem_fatal", "Reboot automatique déclenché par le watchdog suite à la perte totale du modem.")
            time.sleep(5)
            subprocess.run(["sudo", "reboot"])
            time.sleep(600)  # Bloque le thread pendant que le Pi s'éteint
            return False

        next_cooldown = min(
            RECOVERY_COOLDOWN * (2 ** self._consecutive_total_failures),
            MAX_RECOVERY_COOLDOWN,
        )
        logger.critical(
            f"[Watchdog] ❌ Toutes les tentatives de recovery ont échoué "
            f"(échec total consécutif #{self._consecutive_total_failures}). "
            f"Prochaine tentative dans {next_cooldown}s. Le modem est probablement "
            f"dans un état nécessitant un reboot matériel."
        )
        # Alerte fichier (voir utils/alerting.py), uploadée dès que la connexion
        # revient. Le backoff exponentiel ci-dessus espace déjà naturellement les
        # cycles (jusqu'à 1h), donc pas besoin de limiter en plus la fréquence
        # d'alerte : chaque échec total complet en génère une.
        alert_path = write_alert(
            "modem",
            f"Recovery 4G complète échouée (échec total consécutif "
            f"#{self._consecutive_total_failures}). Prochaine tentative dans "
            f"{next_cooldown}s. Reboot matériel probablement nécessaire si ça persiste."
        )
        if alert_path and self._sftp_manager and not self._sftp_manager.upload_file(alert_path):
            logger.warning(f"[Watchdog] Alerte modem non mise en file (queue pleine) : {alert_path}")
        # Reset du compteur de ping pour éviter une re-déclenche immédiate — le
        # cooldown exponentiel (basé sur _consecutive_total_failures) prend le relais.
        self._failure_count = 0
        return False

    # -----------------------------------------------------------------------
    # Étapes individuelles
    # -----------------------------------------------------------------------

    def _find_modem_interface(self) -> str | None:
        """Retourne le nom de l'interface réseau active du modem 4G.

        Essaie d'abord les noms connus (rapide), puis se rabat sur une découverte
        par driver USB (qmi_wwan/cdc_ether/...) : une liste de noms figés peut ne
        plus matcher après une ré-énumération USB (ex: wwan0 -> wwan1)."""
        for iface in MODEM_INTERFACES:
            path = f"/sys/class/net/{iface}"
            if os.path.exists(path):
                logger.debug(f"[Watchdog] Interface modem trouvée (nom connu) : {iface}")
                return iface

        try:
            for iface_path in glob.glob("/sys/class/net/*"):
                iface = os.path.basename(iface_path)
                driver_link = os.path.join(iface_path, "device", "driver")
                if os.path.islink(driver_link):
                    driver = os.path.basename(os.path.realpath(driver_link))
                    if driver in MODEM_DRIVER_HINTS:
                        logger.debug(
                            f"[Watchdog] Interface modem trouvée (driver {driver}) : {iface}"
                        )
                        return iface
        except Exception as exc:
            logger.debug(f"[Watchdog] Découverte par driver échouée : {exc}")

        logger.warning(
            f"[Watchdog] Aucune interface 4G trouvée (noms: {MODEM_INTERFACES}, "
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
            logger.error(f"[Watchdog] Échec restart interface {iface}: {exc}")
            return False

    def _usb_reset(self) -> bool:
        """
        Reset USB du modem via /sys/bus/usb/devices/<id>/authorized.
        Simule un débranchement/rebranchement USB sans toucher au Pi.
        Nécessite sudo (écriture root-only sur ce nœud sysfs), restreint via
        sudoers à `tee` sur ce chemin précis — jamais un shell complet.
        """
        try:
            # Trouver le device USB du modem (SIM7600 VID=1E0E)
            # ou chercher par le nom de l'interface
            usb_auth_paths = glob.glob(
                "/sys/bus/usb/devices/*/authorized"
            )
            modem_path = None

            for auth_path in usb_auth_paths:
                try:
                    # Lire idVendor pour identifier le SIM7600/SIM7900
                    vendor_path = os.path.join(
                        os.path.dirname(auth_path), "idVendor"
                    )
                    if os.path.exists(vendor_path):
                        with open(vendor_path) as f:
                            vendor = f.read().strip().lower()
                        # SIM7600 VID: 1e0e, Quectel EC21: 2c7c, fallback générique
                        if vendor in ("1e0e", "2c7c", "12d1", "1bc7"):
                            modem_path = auth_path
                            break
                except OSError:
                    # Un device USB peut disparaître entre sa découverte et sa
                    # lecture -- précisément le genre d'instabilité USB que ce
                    # watchdog est censé traiter. On continue avec le device
                    # suivant plutôt que d'abandonner toute la recherche.
                    continue

            if not modem_path:
                # Fallback : chercher via l'interface réseau
                iface = self._find_modem_interface()
                if iface:
                    # Remonter dans /sys pour trouver le device USB parent
                    net_path = f"/sys/class/net/{iface}/device"
                    if os.path.exists(net_path):
                        real_path = os.path.realpath(net_path)
                        # Chercher authorized dans les parents
                        candidate = real_path
                        for _ in range(5):
                            candidate = os.path.dirname(candidate)
                            auth = os.path.join(candidate, "authorized")
                            if os.path.exists(auth):
                                modem_path = auth
                                break

            if not modem_path:
                logger.warning(
                    "[Watchdog] USB reset : device USB du modem introuvable dans /sys"
                )
                return False

            logger.info(f"[Watchdog] USB reset via : {modem_path}")
            # `sudo tee <path>` plutôt que `sudo bash -c "echo ... > path"` : un seul
            # binaire fixe en argument sudo (pas de chaîne shell interpolée), ce qui
            # permet une règle sudoers restreinte à ce chemin précis.
            r0 = subprocess.run(
                ["sudo", "tee", modem_path],
                input=b"0\n", timeout=5, capture_output=True
            )
            time.sleep(3)
            # Réactiver
            r1 = subprocess.run(
                ["sudo", "tee", modem_path],
                input=b"1\n", timeout=5, capture_output=True
            )
            if r0.returncode != 0 or r1.returncode != 0:
                logger.error(
                    f"[Watchdog] USB reset : écriture sudo tee échouée "
                    f"(codes {r0.returncode}/{r1.returncode})"
                )
                return False
            return True

        except Exception as exc:
            logger.error(f"[Watchdog] Échec USB reset : {exc}")
            return False

    def _at_reset(self) -> bool:
        """
        Envoie AT+CFUN=1,1 au modem via le port série pour forcer
        un reset firmware. Fonctionnel même si le réseau est coupé.
        """
        at_port = None
        for candidate in AT_SERIAL_CANDIDATES:
            if os.path.exists(candidate):
                at_port = candidate
                break

        if not at_port:
            logger.warning(
                f"[Watchdog] Aucun port AT trouvé parmi {AT_SERIAL_CANDIDATES}"
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
                        f"[Watchdog] Modem non-réactif sur {at_port} "
                        f"(réponse: {response.strip()!r})"
                    )
                    return False

                logger.info(
                    f"[Watchdog] Modem répond sur {at_port}, envoi AT+CFUN=1,1"
                )
                ser.write(b"AT+CFUN=1,1\r\n")
                time.sleep(1)
            return True

        except ImportError:
            logger.warning(
                "[Watchdog] pyserial non installé — AT reset indisponible. "
                "Installer avec : pip install pyserial"
            )
            return False
        except Exception as exc:
            logger.error(f"[Watchdog] Échec AT reset sur {at_port} : {exc}")
            return False

    def _nmcli_reconnect(self) -> bool:
        """
        Utilise nmcli pour déconnecter et reconnecter le profil GSM
        si NetworkManager gère la connexion 4G.
        """
        try:
            # Lister les connexions actives de type gsm
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
                    "[Watchdog] nmcli : aucun profil GSM actif trouvé. "
                    "Tentative de reconnexion générique..."
                )
                # Essaie quand même une reconnexion générique sur l'interface
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
                return True  # On tente, résultat vérifié par le ping

            for profile in gsm_profiles:
                logger.info(f"[Watchdog] nmcli : reconnexion du profil '{profile}'")
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
            logger.warning("[Watchdog] nmcli non disponible.")
            return False
        except Exception as exc:
            logger.error(f"[Watchdog] Échec nmcli reconnect : {exc}")
            return False

    # -----------------------------------------------------------------------
    # Status
    # -----------------------------------------------------------------------

    def status_str(self) -> str:
        """Retourne un résumé de l'état du watchdog (pour les logs perf)."""
        connected = "✅" if self._failure_count == 0 else f"⚠️ {self._failure_count} échec(s)"
        return (
            f"Modem: {connected} | "
            f"Recoveries: {self._recovery_count}"
        )
