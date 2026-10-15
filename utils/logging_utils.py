import csv
import os
import logging
from datetime import datetime

logger = logging.getLogger("Logging")

CPU_TEMP_PATH = "/sys/class/thermal/thermal_zone0/temp"  # standard Raspberry Pi


def read_cpu_temp_c():
    """Retourne la température CPU en °C, ou -1.0 si la lecture échoue.

    -1.0 est une sentinelle sans ambiguïté (température impossible en pratique) :
    avant, un échec de lecture silencieux retournait 0.0/None, indiscernable d'une
    vraie valeur dans le CSV de performance."""
    try:
        with open(CPU_TEMP_PATH, "r") as f:
            milli_c = int(f.read().strip())
        return milli_c / 1000.0
    except Exception as e:
        logger.warning(f"[Logging] Lecture température CPU échouée ({CPU_TEMP_PATH}) : {e}")
        return -1.0


CPU_FREQ_PATH = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"

def read_cpu_freq_mhz():
    """Retourne la fréquence CPU en MHz, ou -1 si la lecture échoue (sentinelle
    sans ambiguïté — avant, un échec silencieux retournait 0, indiscernable d'une
    vraie valeur dans le CSV de performance)."""
    try:
        with open(CPU_FREQ_PATH, "r") as f:
            khz = int(f.read().strip())
        return int(khz / 1000)
    except Exception as e:
        logger.warning(f"[Logging] Lecture fréquence CPU échouée ({CPU_FREQ_PATH}) : {e}")
        return -1


# LOG_DIR vient de utils.configs (source unique) : avant, ce fichier le
# recalculait indépendamment avec la même logique -- ça ne causait pas de bug
# aujourd'hui, mais un futur changement (ex: déplacer les logs vers un disque
# externe) qui n'oublierait de le répercuter qu'ici aurait produit des fichiers
# orphelins, ni nettoyés ni uploadés par le reste du pipeline.
from utils.configs import LOG_DIR

def get_dated_path(prefix, extension="csv"):
    """Generates a dated filename."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(LOG_DIR, f"{prefix}_{ts}.{extension}")

def write_session_metadata(path, camera_model, model_path, tracker_path,
                           imgsz, confidence, capture_fps, extra=None):
    """Ecrit la fiche de session : pose de la camera et configuration.

    Sans elle, les tailles en metres des detections sont irrecuperables des
    qu'on touche au montage : le CSV conserve la boite en coordonnees relatives,
    mais la conversion en metres depend de la hauteur, du tangage, de la focale
    et du capteur, qui n'etaient traces nulle part. Un simple demontage pour
    maintenance rendait toute la campagne anterieure ininterpretable.

    Elle permet aussi de recalculer les tailles a posteriori avec la hauteur
    d'eau reelle, sans rien changer sur le terrain.
    """
    import json as _json
    fiche = {
        "session": os.path.basename(path),
        "date": datetime.now().isoformat(timespec="seconds"),
        "pose": {
            # Le repere doit etre celui du limnimetre : hauteur_m est mesuree
            # au-dessus du zero de l'echelle, pas au-dessus de l'eau du jour.
            "hauteur_m": float(camera_model.position[1]),
            "tangage_deg": float(camera_model.pitch_deg),
            "focale_mm": float(camera_model.focal_length),
            "capteur_mm": [float(v) for v in camera_model.sensor_size],
            "resolution_px": [int(v) for v in camera_model.resolution_px],
        },
        "inference": {
            "modele": str(model_path), "tracker": str(tracker_path),
            "imgsz": imgsz, "confidence": confidence, "capture_fps": capture_fps,
        },
    }
    if extra:
        fiche.update(extra)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            _json.dump(fiche, f, indent=2, ensure_ascii=False)
    except OSError as e:
        # Meme philosophie que les loggers : on n'interrompt jamais l'agent.
        print(f"\u26a0\ufe0f Fiche de session non ecrite ({e})")
    return fiche


class CSVLogger:
    def __init__(self, path=None):
        if path is None:
            self.path = get_dated_path("detections")
        else:
            self.path = path

        # Si l'initialisation échoue (disque plein, FS en lecture seule), le
        # logger passe en mode "broken" : il n'écrit plus rien mais ne fait
        # jamais planter l'appelant. Avant, la moindre OSError ici remontait
        # jusqu'à faire planter tout l'agent (caméra + IA + upload compris).
        self._broken = False
        self._closed = False
        self.file = None
        self.writer = None
        try:
            # Créer dossier logs
            os.makedirs(os.path.dirname(self.path), exist_ok=True)

            # Ouvrir fichier et header si nouveau fichier
            file_exists = os.path.exists(self.path)
            self.file = open(self.path, "a", newline="")
            self.writer = csv.writer(self.file)

            if not file_exists:
                self.writer.writerow([
                    "timestamp",
                    "frame_id",
                    "camera_id",
                    "track_id",
                    "class_id",
                    "confidence",
                    "x1_rel", "y1_rel", "x2_rel", "y2_rel",
                    "width_m", "height_m",
                    "avg_width_m", "avg_height_m",
                    "track_age"
                ])
        except OSError as e:
            logger.error(f"[CSVLogger] Init impossible ({self.path}) : {e} — logging désactivé pour cette session.")
            self._broken = True

    def log_detection(self, camera_id, class_id, conf, bbox, size, avg_size=(0.0, 0.0), track_id=-1, frame_id=-1, track_age=0):
        """Enregistre une détection dans le CSV. No-op silencieux si le logger
        est en mode "broken" (voir __init__/erreur d'écriture précédente)."""
        if self._broken:
            return
        try:
            x1, y1, x2, y2 = bbox
            width_m, height_m = size
            avg_width_m, avg_height_m = avg_size
            ts = datetime.now().isoformat()

            self.writer.writerow([
                ts,
                frame_id,
                camera_id,
                track_id,
                class_id,
                float(conf),
                float(x1), float(y1), float(x2), float(y2),
                float(width_m), float(height_m),
                float(avg_width_m), float(avg_height_m),
                int(track_age)
            ])

            self.file.flush()
            # flush() ne pousse les données que vers le cache du système, pas
            # physiquement sur la carte SD -- fsync() le garantit. Site isolé
            # sans batterie tampon : une coupure secteur brutale peut sinon
            # perdre les dernières détections malgré le flush().
            os.fsync(self.file.fileno())
        except OSError as e:
            logger.error(f"[CSVLogger] Écriture impossible ({self.path}) : {e} — logging désactivé pour cette session.")
            self._broken = True

    def close(self):
        # Idempotent : un close() appelé deux fois (ex: régression future qui
        # oublierait de vérifier l'état avant de recréer un logger) ne fait rien
        # la seconde fois, au lieu de risquer une fuite de descripteur si le
        # premier appel avait été sauté par erreur ailleurs dans le code.
        if self._closed:
            return
        self._closed = True
        if self.file:
            try:
                self.file.close()
            except OSError:
                pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

class PerformanceLogger:
    def __init__(self, path=None):
        if path is None:
            self.path = get_dated_path("performance")
        else:
            self.path = path

        self._broken = False
        self._closed = False
        self._init_file()

    def _init_file(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            if not os.path.exists(self.path):
                with open(self.path, "w", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow(["timestamp", "cam_fps", "inference_ms", "cpu_temp_c", "cpu_load_pct", "cpu_freq_mhz", "ram_usage_pct"])
        except OSError as e:
            logger.error(f"[PerformanceLogger] Init impossible ({self.path}) : {e} — logging désactivé pour cette session.")
            self._broken = True

    def log_stats(self, cam_fps, inference_ms, cpu_temp, cpu_load, ram_usage):
        """No-op silencieux si le logger est en mode "broken" (disque plein/FS
        lecture seule détecté à l'init ou à une écriture précédente) — l'agent
        (caméra, IA, upload) continue de tourner même si ces logs sont perdus."""
        if self._broken:
            return self.path

        freq = read_cpu_freq_mhz()

        try:
            with open(self.path, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now().isoformat(),
                    round(cam_fps, 1),
                    round(inference_ms, 1),
                    round(cpu_temp, 1),
                    round(cpu_load, 1),
                    freq,
                    round(ram_usage, 1)
                ])
                f.flush()
                # Même raisonnement que CSVLogger : flush() seul ne garantit pas
                # une écriture physique sur la carte SD avant une coupure secteur.
                os.fsync(f.fileno())
        except OSError as e:
            logger.error(f"[PerformanceLogger] Écriture impossible ({self.path}) : {e} — logging désactivé pour cette session.")
            self._broken = True
        return self.path

    def close(self):
        # Idempotent pour cohérence avec CSVLogger, même si le fichier est déjà
        # ouvert/fermé à chaque écriture (rien à faire ici aujourd'hui, mais un
        # futur refactor vers un descripteur persistant hériterait de ce garde-fou).
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
