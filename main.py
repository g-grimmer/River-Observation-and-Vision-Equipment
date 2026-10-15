import time
import datetime
import cv2
import os
import sys
import threading
import queue
import signal

# Add current dir to path for imports
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from camera.camera_threads import CameraThread
from detection.yolo_detector import YoloDetector
from detection.tracker import ObjectTracker
from geometry.camera_model import Camera
from utils.disk_cleaner import cleanup_old_files
from utils.configs import (
    FRAME_SIZE, HI_RES_SIZE, VIDEO_FPS, VIDEO_CODEC,
    VIDEO_EXTENSION, CAMERA_IDS, MODEL_PATH_DAY, MODEL_PATH_NIGHT, IMGSZ, CAPTURE_FPS,
    CAMERA_HAUTEUR_M, CAMERA_TANGAGE_DEG, CAMERA_FOCALE_MM, CAMERA_CAPTEUR_MM,
    TRACKER_CONFIG_DAY, TRACKER_CONFIG_NIGHT,
    CONFIDENCE, VIDEO_DIR, LOG_DIR, ROTATION_MINUTES, est_jour
)
from utils.logging_utils import (CSVLogger, PerformanceLogger, read_cpu_temp_c,
                                 write_session_metadata)
from utils.sftp_manager import SFTPManager
from utils.modem_watchdog import ModemWatchdog
from utils.alerting import write_alert

# ---------------------------------------------------------------------------
# Upload modes
# ---------------------------------------------------------------------------
# 'video'          : comportement original — upload vidéo si détections
# 'snapshot'       : Option A — pas de vidéo, seulement JPEG à chaque détection
# 'video+snapshot' : Option B — vidéo enregistrée en local, JPEG uploadé sur 4G
# ---------------------------------------------------------------------------

# Secondes sans frame valide avant de considérer la caméra "active" comme morte
# et de forcer une reconnexion (voir select_camera()). Très supérieur au délai
# de production normal d'une frame (file de 2 + capture ~8 FPS) pour ne jamais
# se déclencher sur un simple ralentissement de l'inférence IA.
CAMERA_HEALTH_TIMEOUT = 15

class VideoRecorder(threading.Thread):
    def __init__(self, filename, size, fps, codec):
        super().__init__(daemon=True)
        self.filename = filename
        self.size = size
        self.fps = fps
        self.codec = codec
        self.queue = queue.Queue(maxsize=100)
        self.running = True

        fourcc = cv2.VideoWriter_fourcc(*codec)
        self.writer = cv2.VideoWriter(filename, fourcc, fps, size)
        print(f"📁 Threaded recording started: {filename}")

    def write(self, frame):
        try:
            self.queue.put_nowait(frame)
        except queue.Full:
            pass

    def run(self):
        while self.running or not self.queue.empty():
            try:
                frame = self.queue.get(timeout=0.2)
                if frame is not None:
                    # On écrit directement la frame, c'est à l'appelant de fournir du BGR
                    self.writer.write(frame)
            except queue.Empty:
                continue
        self.writer.release()
        print(f"✅ Video file closed: {self.filename}")

    def stop(self):
        self.running = False


class TestAgent:
    def __init__(self, keep_all_videos=False, upload_mode='video', model_override=None):
        self.running = True
        self.keep_all_videos = keep_all_videos
        self.upload_mode = upload_mode  # 'video' | 'snapshot' | 'video+snapshot' | 'bench'
        self.active_cam_idx = -1
        self.active_cam_thread = None
        self.session_has_detections = False
        self.camera_fail_count = 0
        self.last_valid_frame_ts = time.time()

        # Snapshot deduplication: track IDs already snapshotted this session
        self.snapshotted_track_ids = set()

        # Modules
        self.sftp = SFTPManager()

        # Watchdog 4G — surveille la connectivité et tente de récupérer
        # le modem sans reboot en cas de freeze (LEDs rouges figées)
        self.modem_watchdog = ModemWatchdog(sftp_manager=self.sftp)
        self.modem_watchdog.start()
        self.logger = None
        self.perf_logger = None
        self.tracker = None

        # Session Tracking
        self.session_ts = ""
        self.start_time = time.time()

        # Pose : voir utils/configs.py
        self.camera_model = Camera(resolution_px=FRAME_SIZE)
        self.camera_model.set_focal_length_and_sensor(
            focal_length=CAMERA_FOCALE_MM, sensor_size=CAMERA_CAPTEUR_MM)
        self.camera_model.position = [0.0, CAMERA_HAUTEUR_M, 0.0]
        self.camera_model.pitch_deg = CAMERA_TANGAGE_DEG

        # AI — bascule jour/nuit automatique (cam 0 diurne / cam 1 nocturne),
        # désactivée si --model impose un modèle fixe (tests manuels/bench).
        self.model_override = model_override
        # Calcule avant le if : `is_day` est relu plus bas pour le tracker, hors
        # de toute branche. Defini seulement dans le else, il levait un
        # NameError des que --model etait passe.
        is_day = est_jour()
        if model_override:
            self.actual_model_path = model_override
        else:
            self.actual_model_path = MODEL_PATH_DAY if is_day else MODEL_PATH_NIGHT
        print(f"🧠 Loading YOLO model: {self.actual_model_path}")
        self.actual_tracker = TRACKER_CONFIG_DAY if is_day else TRACKER_CONFIG_NIGHT
        self.detector = YoloDetector(self.actual_model_path, CONFIDENCE, imgsz=IMGSZ,
                                     tracker=self.actual_tracker)

        # Recording State
        self.recorder = None
        self.frame_id = 0

        # Signal handler
        signal.signal(signal.SIGINT, self.stop_signal)
        signal.signal(signal.SIGTERM, self.stop_signal)

        mode_labels = {
            'video': 'Vidéo (upload vidéo si détections)',
            'snapshot': 'Snapshot (JPEG par détection, pas de vidéo)',
            'video+snapshot': 'Vidéo locale + Snapshot 4G (JPEG par détection)',
        }
        print(f"📡 Mode upload : {mode_labels.get(self.upload_mode, self.upload_mode)}")

    # -----------------------------------------------------------------------
    # Snapshot
    # -----------------------------------------------------------------------

    def _save_detection_snapshot(self, results):
        """
        Capture un JPEG haute résolution via l'ISP hardware de picamera2
        et l'uploade sur le NAS. Un seul snapshot par track ID unique par session.
        """
        if results.boxes is None or results.boxes.id is None:
            return

        # Filtrer les nouveaux track IDs uniquement
        track_ids = [int(tid) for tid in results.boxes.id.tolist()]
        new_ids = [tid for tid in track_ids if tid not in self.snapshotted_track_ids]
        if not new_ids:
            return
        self.snapshotted_track_ids.update(new_ids)

        try:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            snap_filename = f"cam{self.active_cam_idx}_{ts}_snap.jpg"
            snap_path = os.path.join(VIDEO_DIR, snap_filename)

            # --- Capture via array pour éviter le deadlock picamera2 ---
            # Utiliser capture_file pendant qu'on lit déjà des arrays "main" crée un deadlock libcamera
            hires = self.active_cam_thread.picam.capture_array("main")
            # La caméra sort du BGR, OpenCV attend du BGR. Aucune conversion.
            cv2.imwrite(snap_path, hires, [cv2.IMWRITE_JPEG_QUALITY, 85])

            # --- Annotation : boîtes de détection scalées sur la hi-res ---
            # Charger le JPEG qui vient d'être sauvegardé
            frame = cv2.imread(snap_path)
            if frame is not None:
                h_hi, w_hi = frame.shape[:2]
                w_lo, h_lo = FRAME_SIZE  # 640 x 640
                scale_x = w_hi / w_lo
                scale_y = h_hi / h_lo

                for box in results.boxes:
                    x1, y1, x2, y2 = [int(v) for v in box.xyxy[0].tolist()]
                    x1_h = int(x1 * scale_x)
                    y1_h = int(y1 * scale_y)
                    x2_h = int(x2 * scale_x)
                    y2_h = int(y2 * scale_y)
                    conf = float(box.conf[0])
                    cls_id = int(box.cls[0])
                    tid = int(box.id[0]) if box.id is not None else -1

                    cv2.rectangle(frame, (x1_h, y1_h), (x2_h, y2_h), (0, 255, 0), 4)
                    label = f"ID:{tid} cls:{cls_id} {conf:.2f}"
                    cv2.putText(frame, label, (x1_h, max(y1_h - 10, 40)),
                                cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 0), 3)

                # Timestamp en overlay
                ts_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cv2.putText(frame, f"Cam{self.active_cam_idx} | {ts_str}",
                            (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 220, 255), 3)

                cv2.imwrite(snap_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 55])

            size_kb = os.path.getsize(snap_path) / 1024
            print(f"📸 Snapshot: {snap_filename} ({size_kb:.0f} KB) — Track IDs: {new_ids}")
            self.sftp.upload_file(snap_path)

        except Exception as e:
            print(f"⚠️ Snapshot error: {e}")
            import traceback
            traceback.print_exc()

    # -----------------------------------------------------------------------
    # Video helpers
    # -----------------------------------------------------------------------

    def _uses_video(self):
        """True si ce mode enregistre une vidéo localement."""
        return self.upload_mode in ('video', 'video+snapshot', 'bench')

    def _handle_video_on_close(self, old_video):
        """
        Gère le sort d'un fichier vidéo en fin de session selon le mode.
        - 'video'          : upload si détections (ou keep_all), sinon supprime
        - 'video+snapshot' : garde toujours en local, n'uploade jamais
        - 'snapshot'       : old_video est None, ne fait rien
        """
        if not old_video:
            return

        if self.upload_mode == 'video':
            if self.session_has_detections or self.keep_all_videos:
                self.sftp.upload_file(old_video)
                if self.keep_all_videos and not self.session_has_detections:
                    print(f"📁 Vidéo conservée (keep-all): {os.path.basename(old_video)}")
            else:
                try:
                    os.remove(old_video)
                    print(f"🗑️ Vidéo supprimée (pas de détections): {os.path.basename(old_video)}")
                except Exception as e:
                    print(f"⚠️ Impossible de supprimer {os.path.basename(old_video)}: {e}")

        elif self.upload_mode == 'video+snapshot':
            # La vidéo reste en local, jamais uploadée sur 4G
            print(f"💾 Vidéo conservée en local: {os.path.basename(old_video)}")

    # -----------------------------------------------------------------------
    # Session management
    # -----------------------------------------------------------------------

    def _rotate_session(self):
        """Ferme la session courante et en démarre une nouvelle."""
        print(f"🔄 Rotation de session (Cam {self.active_cam_idx})...")

        old_video = self.recorder.filename if self.recorder else None
        old_logger = self.logger.path if self.logger else None
        old_perf = self.perf_logger.path if self.perf_logger else None
        old_meta = getattr(self, "session_meta_path", None)

        self._stop_recording()
        if self.logger: self.logger.close()
        if self.perf_logger: self.perf_logger.close()

        self._handle_video_on_close(old_video)
        if old_logger: self.sftp.upload_file(old_logger)
        if old_perf: self.sftp.upload_file(old_perf)
        # La fiche de pose part avec les donnees qu'elle documente.
        if old_meta: self.sftp.upload_file(old_meta)

        # Nettoyage automatique du disque pour éviter la saturation 24/7
        cleanup_old_files([VIDEO_DIR, LOG_DIR], max_usage_percent=80.0, target_usage_percent=75.0)

        self._start_session()

    def _start_session(self):
        """Démarre une nouvelle session d'enregistrement et de logs."""
        self.session_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.start_time = time.time()
        self.snapshotted_track_ids = set()  # Reset des IDs snapshotés

        # Extraction du dossier (ex: 'model_fusion_26s') et du nom ('best_ncnn_model')
        import os
        model_dir = os.path.basename(os.path.dirname(self.actual_model_path))
        model_file = os.path.splitext(os.path.basename(self.actual_model_path))[0]
        
        if model_dir and model_dir not in [".", ""]:
            model_name = f"{model_dir}_{model_file}"
        else:
            model_name = model_file

        pref = f"cam{self.active_cam_idx}_{model_name}_{self.session_ts}"

        # Init Loggers
        log_path = os.path.join(LOG_DIR, f"{pref}_detections.csv")
        perf_path = os.path.join(LOG_DIR, f"{pref}_performance.csv")
        self.logger = CSVLogger(log_path)
        self.perf_logger = PerformanceLogger(perf_path)

        # Fiche de session : sans la pose, les tailles en metres du CSV ne
        # peuvent plus etre recalculees apres un demontage.
        self.session_meta_path = os.path.join(LOG_DIR, f"{pref}_session.json")
        write_session_metadata(
            self.session_meta_path, self.camera_model, self.actual_model_path,
            getattr(self, "actual_tracker", None), IMGSZ, CONFIDENCE, CAPTURE_FPS)

        # Update tracker
        self.tracker = ObjectTracker(self.logger)

        # Init Recorder (uniquement en mode vidéo)
        if self._uses_video():
            video_path = os.path.join(VIDEO_DIR, f"{pref}.{VIDEO_EXTENSION}")
            self.recorder = VideoRecorder(video_path, HI_RES_SIZE, VIDEO_FPS, VIDEO_CODEC)
            self.recorder.start()
        else:
            self.recorder = None

        self.frame_id = 0
        self.session_has_detections = False

    def select_camera(self):
        """Sélectionne la caméra 0 en régime diurne, la caméra 1 la nuit.

        La plage est définie par est_jour() (utils/configs.py) : 7h-19h le jour,
        19h-7h la nuit.
        """
        now = datetime.datetime.now()
        target_idx = 0 if est_jour(now) else 1

        # Vérification de santé de la caméra déjà active, indépendamment du
        # changement jour/nuit : sans ça, une caméra qui meurt EN COURS de
        # fonctionnement (câble CSI délogé, alimentation instable) n'était plus
        # jamais re-détectée avant le prochain changement de créneau (jusqu'à
        # 7h ou 19h) puisque tout le bloc suivant ne s'exécute que si
        # active_cam_idx != target_idx.
        if (self.active_cam_idx == target_idx and self.active_cam_thread is not None
                and time.time() - self.last_valid_frame_ts > CAMERA_HEALTH_TIMEOUT):
            print(
                f"❌ Caméra {self.active_cam_idx} muette depuis plus de "
                f"{CAMERA_HEALTH_TIMEOUT}s — reconnexion forcée."
            )
            self.active_cam_thread.stop()
            self.active_cam_thread = None
            self.active_cam_idx = -1  # force la reconstruction ci-dessous

        if self.active_cam_idx != target_idx:
            print(f"🔄 Passage à la Caméra {target_idx} (heure: {now.hour}h)")

            if self.recorder or self.logger:
                old_video = self.recorder.filename if self.recorder else None
                old_logger = self.logger.path if self.logger else None
                old_perf = self.perf_logger.path if self.perf_logger else None

                self._stop_recording()
                if self.logger: self.logger.close()
                if self.perf_logger: self.perf_logger.close()

                self._handle_video_on_close(old_video)
                if old_logger: self.sftp.upload_file(old_logger)
                if old_perf: self.sftp.upload_file(old_perf)

                # Nettoyage automatique du disque
                cleanup_old_files([VIDEO_DIR, LOG_DIR], max_usage_percent=80.0, target_usage_percent=75.0)

            if self.active_cam_thread:
                self.active_cam_thread.stop()
                self.active_cam_thread = None

            # Init caméra protégée : une caméra absente/débranchée ne doit jamais
            # faire crasher tout l'agent — elle sera retentée au prochain check (60s).
            try:
                new_cam_thread = CameraThread(CAMERA_IDS[target_idx], FRAME_SIZE, HI_RES_SIZE)
                new_cam_thread.start()
                new_cam_thread.enable()
            except Exception as e:
                print(f"❌ Caméra {target_idx} indisponible ({e}) — nouvelle tentative dans 60s.")
                self.active_cam_thread = None
                self.active_cam_idx = -1
                self.camera_fail_count += 1
                # Alerte au bout de ~5min, ~30min, puis toutes les heures — évite de
                # spammer le NAS à chaque tentative tout en gardant une trace remontée.
                if self.camera_fail_count in (5, 30) or self.camera_fail_count % 60 == 0:
                    alert_path = write_alert(
                        "camera",
                        f"Caméra {target_idx} indisponible depuis {self.camera_fail_count} "
                        f"tentatives consécutives (~{self.camera_fail_count} min) : {e}"
                    )
                    if alert_path and not self.sftp.upload_file(alert_path):
                        print(f"⚠️ Alerte caméra non mise en file (queue pleine) : {alert_path}")
                return

            self.camera_fail_count = 0
            self.active_cam_thread = new_cam_thread
            self.active_cam_idx = target_idx
            # Repart d'une horloge fraîche : évite de redéclencher immédiatement
            # le contrôle de santé avant que la nouvelle caméra n'ait eu le temps
            # de produire sa première frame.
            self.last_valid_frame_ts = time.time()

            # Bascule automatique du modèle IA jour/nuit (sauf --model fixe)
            if not self.model_override:
                target_model_path = MODEL_PATH_DAY if target_idx == 0 else MODEL_PATH_NIGHT
                target_tracker = TRACKER_CONFIG_DAY if target_idx == 0 else TRACKER_CONFIG_NIGHT
                if target_model_path != self.actual_model_path:
                    print(f"🧠 Changement de modèle IA : {target_model_path}")
                    try:
                        # imgsz et tracker doivent être transmis ici aussi : sans
                        # imgsz, Ultralytics retombait sur 640 après la première
                        # bascule et le modèle 960 travaillait hors résolution,
                        # sans le moindre message.
                        new_detector = YoloDetector(target_model_path, CONFIDENCE,
                                                    imgsz=IMGSZ, tracker=target_tracker)
                        self.detector = new_detector
                        self.actual_model_path = target_model_path
                        self.actual_tracker = target_tracker
                    except Exception as e:
                        print(
                            f"❌ Impossible de charger {target_model_path} ({e}) — "
                            f"on continue avec {self.actual_model_path}."
                        )

            time.sleep(1.0)
            self._start_session()

    def _stop_recording(self):
        if getattr(self, 'recorder', None):
            self.recorder.stop()
            self.recorder.join()
            self.recorder = None

    # -----------------------------------------------------------------------
    # Main loop
    # -----------------------------------------------------------------------

    def run(self):
        mode_str = {
            'video': f"Production vidéo (rotation {ROTATION_MINUTES}min)",
            'snapshot': "Snapshot uniquement (JPEG par détection, pas de vidéo)",
            'video+snapshot': f"Vidéo locale + Snapshot 4G (rotation {ROTATION_MINUTES}min)",
            'bench': "Benchmark HardwareX (Logs exhaustifs + Vidéo annotée)",
        }.get(self.upload_mode, self.upload_mode)
        if self.upload_mode == 'video' and self.keep_all_videos:
            mode_str += " | CONSERVATION SYSTÉMATIQUE"

        print(f"🚀 Agent démarré | Mode: {mode_str}")
        last_cam_check = 0
        last_perf_log = 0
        fatal_error = False

        try:
            while self.running:
                now = time.time()

                # Vérifier la caméra toutes les minutes
                if now - last_cam_check > 60:
                    self.select_camera()
                    last_cam_check = now

                if self.active_cam_thread is None:
                    time.sleep(0.1)
                    continue

                # --- ROTATION (pour uploader les CSV et vidéos selon le mode) ---
                if now - self.start_time > (ROTATION_MINUTES * 60):
                    self._rotate_session()

                # 1. Capture lores pour l'IA
                lores_frame = self.active_cam_thread.read()
                if lores_frame is None:
                    time.sleep(0.005)
                    continue

                self.last_valid_frame_ts = now  # alimente le contrôle de santé de select_camera()
                self.frame_id += 1

                # 2. Capture hi-res pour l'enregistrement vidéo (mode vidéo uniquement)
                if self.recorder:
                    try:
                        hires_frame = self.active_cam_thread.picam.capture_array("main")
                        # La caméra sort du BGR, la vidéo attend du BGR. Aucune conversion.
                        self.recorder.write(hires_frame)
                    except Exception as e:
                        print(f"⚠️ Erreur capture main frame: {e}")

                # 3. Inférence IA. Ultralytics inverse lui-même les canaux dans
                # Predictor.preprocess() (`im = im[..., ::-1]  # BGR to RGB`) :
                # il attend donc du BGR, exactement ce que sort la caméra.
                # Convertir en RGB ici faisait parvenir au réseau des canaux
                # inversés -- sans erreur visible, mais avec un modèle dégradé.
                t_inf_start = time.time()
                results = self.detector.predict(lores_frame)[0]
                inf_ms = (time.time() - t_inf_start) * 1000

                # 4. Détection confirmée par ByteTrack
                if results.boxes.id is not None:
                    if not self.session_has_detections:
                        detected_classes = (
                            [int(cls) for cls in results.boxes.cls.tolist()]
                            if hasattr(results.boxes, 'cls') else []
                        )
                        print(f"👁️ ByteTrack a confirmé des objets (Classes: {detected_classes}).")
                        if self.upload_mode == 'video':
                            print(f"   → Vidéo marquée pour upload SFTP.")
                        elif self.upload_mode in ('snapshot', 'video+snapshot'):
                            print(f"   → Snapshot JPEG en cours...")

                    self.session_has_detections = True

                    # Snapshot JPEG (modes snapshot et video+snapshot)
                    if self.upload_mode in ('snapshot', 'video+snapshot'):
                        self._save_detection_snapshot(results)

                # 5. Tracking & Logging
                if self.tracker:
                    is_bench = (self.upload_mode == 'bench')
                    annotated, _ = self.tracker.update(
                        results, self.active_cam_idx, self.camera_model,
                        log_every_frame=not is_bench, frame_id=self.frame_id,
                        log_all_occurrences=is_bench
                    )
                    self.tracker.check_lost_tracks()

                # 6. Perf Logging (toutes les 10s)
                if now - last_perf_log > 10:
                    import psutil
                    cam_fps = self.active_cam_thread.actual_fps if self.active_cam_thread else 0.0
                    if self.perf_logger:
                        self.perf_logger.log_stats(
                            cam_fps,
                            inf_ms,
                            read_cpu_temp_c(),
                            psutil.cpu_percent(),
                            psutil.virtual_memory().percent
                        )
                    last_perf_log = now
                    session_min = int((now - self.start_time) / 60)
                    wd_status = self.modem_watchdog.status_str() if self.modem_watchdog else ""
                    print(f"📊 [Cam {self.active_cam_idx}] FPS: {cam_fps:.1f} | "
                          f"AI: {inf_ms:.1f}ms | Frame: {self.frame_id} | "
                          f"Session: {session_min}min | "
                          f"Snaps: {len(self.snapshotted_track_ids)} | {wd_status}")

        except Exception as e:
            print(f"🔥 Erreur fatale dans la boucle principale: {e}")
            import traceback
            traceback.print_exc()
            fatal_error = True
            # Avant, seuls les chemins caméra/modem déclenchaient une alerte : un
            # bug logiciel qui crash-loop en continu (exactement le scénario que
            # ce mécanisme d'alerte doit couvrir) ne produisait aucun signal
            # distant. Limité à un fichier marqueur horodaté (pas un compteur en
            # mémoire) car chaque crash recrée une toute nouvelle instance de
            # TestAgent -- un compteur d'instance ne survivrait pas au redémarrage.
            if self._should_alert_fatal():
                alert_path = write_alert("fatal", f"Erreur fatale dans la boucle principale : {e}")
                if alert_path and not self.sftp.upload_file(alert_path):
                    print(f"⚠️ Alerte fatale non mise en file (queue pleine) : {alert_path}")
        finally:
            self.stop()

        if fatal_error:
            # Code de sortie non-nul : run_agent.sh distingue un vrai crash
            # d'un arrêt volontaire (Ctrl+C/SIGTERM -> exit 0) et applique son
            # propre backoff au lieu de laisser systemd seul relancer en boucle.
            sys.exit(1)

    # -----------------------------------------------------------------------
    # Utilities
    # -----------------------------------------------------------------------

    def _should_alert_fatal(self, min_interval=300):
        """Limite la fréquence des alertes 'fatal' à travers les redémarrages
        du process (chaque crash recrée une toute nouvelle instance de
        TestAgent, donc un compteur en mémoire ne suffit pas) via un fichier
        marqueur horodaté. Par défaut, au plus une alerte toutes les 5 minutes
        même si l'agent crash-loop beaucoup plus vite que ça."""
        marker = os.path.join(LOG_DIR, ".last_fatal_alert")
        try:
            if os.path.exists(marker) and time.time() - os.path.getmtime(marker) < min_interval:
                return False
            with open(marker, "w") as f:
                f.write(str(time.time()))
            return True
        except OSError:
            return True  # en cas de doute, mieux vaut alerter que rester silencieux

    def stop_signal(self, signum, frame):
        # Ne fait QUE positionner le flag d'arrêt : le nettoyage complet (fermeture
        # des loggers, upload final...) se fait une seule fois via le `finally` de
        # run(), dans le fil d'exécution normal de la boucle principale. Fermer
        # les fichiers directement ici pourrait interrompre une écriture CSV en
        # cours (le signal peut arriver à tout moment, y compris en pleine
        # itération de tracker.update()), provoquant un ValueError sur fichier
        # déjà fermé -- et une fausse "erreur fatale" à chaque redémarrage normal.
        self.running = False

    def stop(self):
        print("🛑 Arrêt de l'Agent...")
        self.running = False

        if self.recorder or self.logger:
            v = self.recorder.filename if self.recorder else None
            l = self.logger.path if self.logger else None
            p = self.perf_logger.path if self.perf_logger else None

            self._stop_recording()
            if self.logger: self.logger.close()
            if self.perf_logger: self.perf_logger.close()

            self._handle_video_on_close(v)
            if l: self.sftp.upload_file(l)
            if p: self.sftp.upload_file(p)

        if self.active_cam_thread:
            self.active_cam_thread.stop()

        if self.modem_watchdog:
            self.modem_watchdog.stop()

        if self.sftp:
            self.sftp.stop()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Agent Alpha 1 Test Terrain")

    parser.add_argument(
        "--keep-all",
        action="store_true",
        help="[Mode 'video' uniquement] Conserver et envoyer toutes les vidéos, "
             "même sans détection."
    )
    parser.add_argument(
        "--upload-mode",
        choices=["video", "snapshot", "video+snapshot", "bench"],
        default="video",
        help=(
            "Mode d'upload 4G :\n"
            "  video          : upload vidéo si détections (comportement original)\n"
            "  snapshot       : JPEG annoté par détection uniquement, pas de vidéo\n"
            "  video+snapshot : vidéo enregistrée en local + JPEG uploadé sur 4G\n"
            "  bench          : pour analyse HardwareX (log exhaustif + vidéo annotée locale)"
        )
    )

    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Surcharge le MODEL_PATH de configs.py (pratique pour tester NCNN/ONNX)"
    )

    args = parser.parse_args()

    try:
        agent = TestAgent(
            keep_all_videos=args.keep_all,
            upload_mode=args.upload_mode,
            model_override=args.model,
        )
    except Exception as e:
        # Une erreur ici (avant même que la boucle principale n'existe) était
        # jusqu'ici totalement invisible à distance : write_alert() n'était
        # appelé que depuis l'intérieur de run(). On écrit au moins le fichier
        # localement -- il sera repris et uploadé au prochain démarrage réussi
        # via le rescan de SFTPManager -- et on sort avec un code non-nul pour
        # que run_agent.sh applique son backoff plutôt que de relancer aussitôt.
        print(f"🔥 Erreur fatale à l'initialisation : {e}")
        import traceback
        traceback.print_exc()
        write_alert("fatal_init", f"Erreur fatale à l'initialisation de l'agent : {e}")
        sys.exit(1)

    agent.run()
