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
# 'video'          : original behaviour -- upload the video if there were detections
# 'snapshot'       : Option A -- no video, only a JPEG per detection
# 'video+snapshot' : Option B -- video recorded locally, JPEG uploaded over 4G
# ---------------------------------------------------------------------------

# Seconds without a valid frame before the "active" camera is considered dead
# and a reconnection is forced (see select_camera()). Far above the normal
# production delay of a frame (queue of 2 + capture at ~8 FPS) so that it never
# fires on a mere slowdown of the AI inference.
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
                    # The frame is written as-is; it is up to the caller to supply BGR
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

        # 4G watchdog -- monitors connectivity and tries to recover the modem
        # without rebooting when it freezes (steady red LEDs)
        self.modem_watchdog = ModemWatchdog(sftp_manager=self.sftp)
        self.modem_watchdog.start()
        self.logger = None
        self.perf_logger = None
        self.tracker = None

        # Session Tracking
        self.session_ts = ""
        self.start_time = time.time()

        # Pose: see utils/configs.py
        self.camera_model = Camera(resolution_px=FRAME_SIZE)
        self.camera_model.set_focal_length_and_sensor(
            focal_length=CAMERA_FOCALE_MM, sensor_size=CAMERA_CAPTEUR_MM)
        self.camera_model.position = [0.0, CAMERA_HAUTEUR_M, 0.0]
        self.camera_model.pitch_deg = CAMERA_TANGAGE_DEG

        # AI -- automatic day/night switch (cam 0 diurnal / cam 1 nocturnal),
        # disabled when --model forces a fixed model (manual tests / bench).
        self.model_override = model_override
        # Computed before the if: `is_day` is read again below for the tracker,
        # outside any branch. Defined only in the else, it raised a NameError as
        # soon as --model was passed.
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
            'video': 'Video (upload the video if there were detections)',
            'snapshot': 'Snapshot (one JPEG per detection, no video)',
            'video+snapshot': 'Local video + 4G snapshot (one JPEG per detection)',
        }
        print(f"📡 Upload mode: {mode_labels.get(self.upload_mode, self.upload_mode)}")

    # -----------------------------------------------------------------------
    # Snapshot
    # -----------------------------------------------------------------------

    def _save_detection_snapshot(self, results):
        """
        Capture a high-resolution JPEG through the picamera2 hardware ISP and
        upload it to the NAS. One snapshot per unique track ID per session.
        """
        if results.boxes is None or results.boxes.id is None:
            return

        # Keep only the new track IDs
        track_ids = [int(tid) for tid in results.boxes.id.tolist()]
        new_ids = [tid for tid in track_ids if tid not in self.snapshotted_track_ids]
        if not new_ids:
            return
        self.snapshotted_track_ids.update(new_ids)

        try:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            snap_filename = f"cam{self.active_cam_idx}_{ts}_snap.jpg"
            snap_path = os.path.join(VIDEO_DIR, snap_filename)

            # --- Capture through an array, to avoid the picamera2 deadlock ---
            # Using capture_file while already reading "main" arrays deadlocks libcamera
            hires = self.active_cam_thread.picam.capture_array("main")
            # The camera outputs BGR and OpenCV expects BGR. No conversion.
            cv2.imwrite(snap_path, hires, [cv2.IMWRITE_JPEG_QUALITY, 85])

            # --- Annotation: detection boxes scaled onto the hi-res image ---
            # Load the JPEG that has just been saved
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

                # Timestamp overlay
                ts_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                cv2.putText(frame, f"Cam{self.active_cam_idx} | {ts_str}",
                            (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.5, (0, 220, 255), 3)

                cv2.imwrite(snap_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 55])

            size_kb = os.path.getsize(snap_path) / 1024
            print(f"📸 Snapshot: {snap_filename} ({size_kb:.0f} KB) -- Track IDs: {new_ids}")
            self.sftp.upload_file(snap_path)

        except Exception as e:
            print(f"⚠️ Snapshot error: {e}")
            import traceback
            traceback.print_exc()

    # -----------------------------------------------------------------------
    # Video helpers
    # -----------------------------------------------------------------------

    def _uses_video(self):
        """True if this mode records a video locally."""
        return self.upload_mode in ('video', 'video+snapshot', 'bench')

    def _handle_video_on_close(self, old_video):
        """
        Decide the fate of a video file at the end of a session, per mode.
        - 'video'          : upload if there were detections (or keep_all), else delete
        - 'video+snapshot' : always kept locally, never uploaded
        - 'snapshot'       : old_video is None, nothing to do
        """
        if not old_video:
            return

        if self.upload_mode == 'video':
            if self.session_has_detections or self.keep_all_videos:
                self.sftp.upload_file(old_video)
                if self.keep_all_videos and not self.session_has_detections:
                    print(f"📁 Video kept (keep-all): {os.path.basename(old_video)}")
            else:
                try:
                    os.remove(old_video)
                    print(f"🗑️ Video deleted (no detections): {os.path.basename(old_video)}")
                except Exception as e:
                    print(f"⚠️ Could not delete {os.path.basename(old_video)}: {e}")

        elif self.upload_mode == 'video+snapshot':
            # The video stays local, never uploaded over 4G
            print(f"💾 Video kept locally: {os.path.basename(old_video)}")

    # -----------------------------------------------------------------------
    # Session management
    # -----------------------------------------------------------------------

    def _rotate_session(self):
        """Close the current session and start a new one."""
        print(f"🔄 Session rotation (Cam {self.active_cam_idx})...")

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
        # The pose record travels with the data it documents.
        if old_meta: self.sftp.upload_file(old_meta)

        # Automatic disk cleanup, to avoid 24/7 saturation
        cleanup_old_files([VIDEO_DIR, LOG_DIR], max_usage_percent=80.0, target_usage_percent=75.0)

        self._start_session()

    def _start_session(self):
        """Start a new recording and logging session."""
        self.session_ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.start_time = time.time()
        self.snapshotted_track_ids = set()  # Reset the snapshotted IDs

        # Extract the folder (e.g. 'model_fusion_26s') and the name ('best_ncnn_model')
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

        # Session record: without the pose, the sizes in metres in the CSV can
        # no longer be recomputed after the mount is dismantled.
        self.session_meta_path = os.path.join(LOG_DIR, f"{pref}_session.json")
        write_session_metadata(
            self.session_meta_path, self.camera_model, self.actual_model_path,
            getattr(self, "actual_tracker", None), IMGSZ, CONFIDENCE, CAPTURE_FPS)

        # Update tracker
        self.tracker = ObjectTracker(self.logger)

        # Init Recorder (video modes only)
        if self._uses_video():
            video_path = os.path.join(VIDEO_DIR, f"{pref}.{VIDEO_EXTENSION}")
            self.recorder = VideoRecorder(video_path, HI_RES_SIZE, VIDEO_FPS, VIDEO_CODEC)
            self.recorder.start()
        else:
            self.recorder = None

        self.frame_id = 0
        self.session_has_detections = False

    def select_camera(self):
        """Select camera 0 in the diurnal regime, camera 1 at night.

        The window is defined by est_jour() (utils/configs.py): 07:00-19:00 by
        day, 19:00-07:00 at night.
        """
        now = datetime.datetime.now()
        target_idx = 0 if est_jour(now) else 1

        # Health check of the already active camera, independently of the
        # day/night switch: without it, a camera dying WHILE running (CSI cable
        # dislodged, unstable power) was never detected again before the next
        # window change (up to 07:00 or 19:00), since the whole block below only
        # runs when active_cam_idx != target_idx.
        if (self.active_cam_idx == target_idx and self.active_cam_thread is not None
                and time.time() - self.last_valid_frame_ts > CAMERA_HEALTH_TIMEOUT):
            print(
                f"❌ Camera {self.active_cam_idx} silent for more than "
                f"{CAMERA_HEALTH_TIMEOUT}s -- forcing a reconnection."
            )
            self.active_cam_thread.stop()
            self.active_cam_thread = None
            self.active_cam_idx = -1  # force the rebuild below

        if self.active_cam_idx != target_idx:
            print(f"🔄 Switching to camera {target_idx} (hour: {now.hour}h)")

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

                # Automatic disk cleanup
                cleanup_old_files([VIDEO_DIR, LOG_DIR], max_usage_percent=80.0, target_usage_percent=75.0)

            if self.active_cam_thread:
                self.active_cam_thread.stop()
                self.active_cam_thread = None

            # Protected camera init: a missing or unplugged camera must never
            # crash the whole agent -- it is retried at the next check (60s).
            try:
                new_cam_thread = CameraThread(CAMERA_IDS[target_idx], FRAME_SIZE, HI_RES_SIZE)
                new_cam_thread.start()
                new_cam_thread.enable()
            except Exception as e:
                print(f"❌ Camera {target_idx} unavailable ({e}) -- retrying in 60s.")
                self.active_cam_thread = None
                self.active_cam_idx = -1
                self.camera_fail_count += 1
                # Alert after ~5 min, ~30 min, then hourly -- avoids spamming the
                # NAS on every attempt while still leaving a remote trace.
                if self.camera_fail_count in (5, 30) or self.camera_fail_count % 60 == 0:
                    alert_path = write_alert(
                        "camera",
                        f"Camera {target_idx} unavailable for {self.camera_fail_count} "
                        f"consecutive attempts (~{self.camera_fail_count} min): {e}"
                    )
                    if alert_path and not self.sftp.upload_file(alert_path):
                        print(f"⚠️ Camera alert not queued (queue full): {alert_path}")
                return

            self.camera_fail_count = 0
            self.active_cam_thread = new_cam_thread
            self.active_cam_idx = target_idx
            # Restart from a fresh clock: avoids re-triggering the health check
            # immediately, before the new camera has had time to produce its
            # first frame.
            self.last_valid_frame_ts = time.time()

            # Automatic day/night AI model switch (unless --model is fixed)
            if not self.model_override:
                target_model_path = MODEL_PATH_DAY if target_idx == 0 else MODEL_PATH_NIGHT
                target_tracker = TRACKER_CONFIG_DAY if target_idx == 0 else TRACKER_CONFIG_NIGHT
                if target_model_path != self.actual_model_path:
                    print(f"🧠 Switching AI model: {target_model_path}")
                    try:
                        # imgsz and tracker must be passed here too: without
                        # imgsz, Ultralytics fell back to 640 after the first
                        # switch and the 960 model ran out of its resolution,
                        # without a single message.
                        new_detector = YoloDetector(target_model_path, CONFIDENCE,
                                                    imgsz=IMGSZ, tracker=target_tracker)
                        self.detector = new_detector
                        self.actual_model_path = target_model_path
                        self.actual_tracker = target_tracker
                    except Exception as e:
                        print(
                            f"❌ Could not load {target_model_path} ({e}) -- "
                            f"carrying on with {self.actual_model_path}."
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
            'video': f"Video production (rotation every {ROTATION_MINUTES} min)",
            'snapshot': "Snapshot only (one JPEG per detection, no video)",
            'video+snapshot': f"Local video + 4G snapshot (rotation every {ROTATION_MINUTES} min)",
            'bench': "HardwareX benchmark (exhaustive logs + annotated video)",
        }.get(self.upload_mode, self.upload_mode)
        if self.upload_mode == 'video' and self.keep_all_videos:
            mode_str += " | KEEP EVERYTHING"

        print(f"🚀 Agent started | Mode: {mode_str}")
        last_cam_check = 0
        last_perf_log = 0
        fatal_error = False

        try:
            while self.running:
                now = time.time()

                # Check the camera every minute
                if now - last_cam_check > 60:
                    self.select_camera()
                    last_cam_check = now

                if self.active_cam_thread is None:
                    time.sleep(0.1)
                    continue

                # --- ROTATION (to upload the CSV files and videos per mode) ---
                if now - self.start_time > (ROTATION_MINUTES * 60):
                    self._rotate_session()

                # 1. Lores capture for the AI
                lores_frame = self.active_cam_thread.read()
                if lores_frame is None:
                    time.sleep(0.005)
                    continue

                self.last_valid_frame_ts = now  # feeds the health check of select_camera()
                self.frame_id += 1

                # 2. Hi-res capture for video recording (video modes only)
                if self.recorder:
                    try:
                        hires_frame = self.active_cam_thread.picam.capture_array("main")
                        # The camera outputs BGR and the video expects BGR. No conversion.
                        self.recorder.write(hires_frame)
                    except Exception as e:
                        print(f"⚠️ Main frame capture error: {e}")

                # 3. AI inference. Ultralytics swaps the channels itself in
                # Predictor.preprocess() (`im = im[..., ::-1]  # BGR to RGB`):
                # it therefore expects BGR, exactly what the camera outputs.
                # Converting to RGB here fed the network inverted channels --
                # with no visible error, but with a degraded model.
                t_inf_start = time.time()
                results = self.detector.predict(lores_frame)[0]
                inf_ms = (time.time() - t_inf_start) * 1000

                # 4. Detection confirmed by ByteTrack
                if results.boxes.id is not None:
                    if not self.session_has_detections:
                        detected_classes = (
                            [int(cls) for cls in results.boxes.cls.tolist()]
                            if hasattr(results.boxes, 'cls') else []
                        )
                        print(f"👁️ ByteTrack confirmed objects (classes: {detected_classes}).")
                        if self.upload_mode == 'video':
                            print(f"   -> Video marked for SFTP upload.")
                        elif self.upload_mode in ('snapshot', 'video+snapshot'):
                            print(f"   -> Writing the JPEG snapshot...")

                    self.session_has_detections = True

                    # JPEG snapshot (snapshot and video+snapshot modes)
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
            print(f"🔥 Fatal error in the main loop: {e}")
            import traceback
            traceback.print_exc()
            fatal_error = True
            # Before, only the camera and modem paths raised an alert: a software
            # bug crash-looping continuously (exactly the scenario this alert
            # mechanism is meant to cover) produced no remote signal at all.
            # Limited by a timestamped marker file (not an in-memory counter)
            # because every crash creates a brand new TestAgent instance -- an
            # instance counter would not survive the restart.
            if self._should_alert_fatal():
                alert_path = write_alert("fatal", f"Fatal error in the main loop: {e}")
                if alert_path and not self.sftp.upload_file(alert_path):
                    print(f"⚠️ Fatal alert not queued (queue full): {alert_path}")
        finally:
            self.stop()

        if fatal_error:
            # Non-zero exit code: run_agent.sh distinguishes a real crash from a
            # deliberate stop (Ctrl+C/SIGTERM -> exit 0) and applies its own
            # backoff instead of leaving systemd alone to restart in a loop.
            sys.exit(1)

    # -----------------------------------------------------------------------
    # Utilities
    # -----------------------------------------------------------------------

    def _should_alert_fatal(self, min_interval=300):
        """Rate-limit the 'fatal' alerts across process restarts (every crash
        creates a brand new TestAgent instance, so an in-memory counter is not
        enough) through a timestamped marker file. By default, at most one alert
        every 5 minutes even if the agent crash-loops much faster than that."""
        marker = os.path.join(LOG_DIR, ".last_fatal_alert")
        try:
            if os.path.exists(marker) and time.time() - os.path.getmtime(marker) < min_interval:
                return False
            with open(marker, "w") as f:
                f.write(str(time.time()))
            return True
        except OSError:
            return True  # when in doubt, better to alert than to stay silent

    def stop_signal(self, signum, frame):
        # ONLY sets the stop flag: the full cleanup (closing the loggers, final
        # upload...) happens exactly once through the `finally` of run(), in the
        # normal execution path of the main loop. Closing the files directly here
        # could interrupt a CSV write in progress (the signal can arrive at any
        # moment, including in the middle of tracker.update()), causing a
        # ValueError on an already closed file -- and a bogus "fatal error" on
        # every normal restart.
        self.running = False

    def stop(self):
        print("🛑 Stopping the agent...")
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
        help="['video' mode only] Keep and send every video, "
             "even without detections."
    )
    parser.add_argument(
        "--upload-mode",
        choices=["video", "snapshot", "video+snapshot", "bench"],
        default="video",
        help=(
            "4G upload mode:\n"
            "  video          : upload the video if there were detections (original behaviour)\n"
            "  snapshot       : annotated JPEG per detection only, no video\n"
            "  video+snapshot : video recorded locally + JPEG uploaded over 4G\n"
            "  bench          : for HardwareX analysis (exhaustive log + local annotated video)"
        )
    )

    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Override MODEL_PATH from configs.py (handy to test NCNN/ONNX)"
    )

    args = parser.parse_args()

    try:
        agent = TestAgent(
            keep_all_videos=args.keep_all,
            upload_mode=args.upload_mode,
            model_override=args.model,
        )
    except Exception as e:
        # An error here (before the main loop even exists) used to be completely
        # invisible remotely: write_alert() was only called from inside run().
        # We now at least write the file locally -- it will be picked up and
        # uploaded at the next successful start through the SFTPManager rescan --
        # and we exit with a non-zero code so that run_agent.sh applies its
        # backoff rather than restarting immediately.
        print(f"🔥 Fatal error during initialisation: {e}")
        import traceback
        traceback.print_exc()
        write_alert("fatal_init", f"Fatal error during agent initialisation: {e}")
        sys.exit(1)

    agent.run()
