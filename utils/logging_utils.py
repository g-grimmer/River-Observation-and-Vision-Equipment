import csv
import os
import logging
from datetime import datetime

logger = logging.getLogger("Logging")

CPU_TEMP_PATH = "/sys/class/thermal/thermal_zone0/temp"  # standard Raspberry Pi


def read_cpu_temp_c():
    """Return the CPU temperature in degrees C, or -1.0 if the read fails.

    -1.0 is an unambiguous sentinel (a temperature impossible in practice):
    before, a silent read failure returned 0.0/None, indistinguishable from a
    real value in the performance CSV."""
    try:
        with open(CPU_TEMP_PATH, "r") as f:
            milli_c = int(f.read().strip())
        return milli_c / 1000.0
    except Exception as e:
        logger.warning(f"[Logging] CPU temperature read failed ({CPU_TEMP_PATH}): {e}")
        return -1.0


CPU_FREQ_PATH = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"

def read_cpu_freq_mhz():
    """Return the CPU frequency in MHz, or -1 if the read fails (an unambiguous
    sentinel -- before, a silent failure returned 0, indistinguishable from a
    real value in the performance CSV)."""
    try:
        with open(CPU_FREQ_PATH, "r") as f:
            khz = int(f.read().strip())
        return int(khz / 1000)
    except Exception as e:
        logger.warning(f"[Logging] CPU frequency read failed ({CPU_FREQ_PATH}): {e}")
        return -1


# LOG_DIR comes from utils.configs (single source): before, this file
# recomputed it independently with the same logic -- that caused no bug today,
# but a future change (e.g. moving the logs to an external disk) that forgot to
# mirror it here would have produced orphaned files, neither cleaned up nor
# uploaded by the rest of the pipeline.
from utils.configs import LOG_DIR

def get_dated_path(prefix, extension="csv"):
    """Generates a dated filename."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return os.path.join(LOG_DIR, f"{prefix}_{ts}.{extension}")

def write_session_metadata(path, camera_model, model_path, tracker_path,
                           imgsz, confidence, capture_fps, extra=None):
    """Write the session record: camera pose and configuration.

    Without it, the detection sizes in metres are unrecoverable as soon as the
    mount is disturbed: the CSV keeps the box in relative coordinates, but the
    conversion to metres depends on height, tilt, focal length and sensor, which
    were traced nowhere. A simple dismantling for maintenance made the whole
    preceding campaign uninterpretable.

    It also allows sizes to be recomputed afterwards with the real water level,
    without changing anything in the field.
    """
    import json as _json
    fiche = {
        "session": os.path.basename(path),
        "date": datetime.now().isoformat(timespec="seconds"),
        "pose": {
            # The datum must be the staff gauge: hauteur_m is measured above
            # the gauge zero, not above the water on the day.
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
        # Same philosophy as the loggers: the agent is never interrupted.
        print(f"\u26a0\ufe0f Session record not written ({e})")
    return fiche


class CSVLogger:
    def __init__(self, path=None):
        if path is None:
            self.path = get_dated_path("detections")
        else:
            self.path = path

        # If initialisation fails (disk full, read-only FS), the logger
        # switches to "broken" mode: it writes nothing any more but never
        # crashes the caller. Before, the slightest OSError here propagated all
        # the way up and crashed the whole agent (camera, AI and upload included).
        self._broken = False
        self._closed = False
        self.file = None
        self.writer = None
        try:
            # Create the logs directory
            os.makedirs(os.path.dirname(self.path), exist_ok=True)

            # Open the file, and write the header if it is a new file
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
            logger.error(f"[CSVLogger] Init failed ({self.path}): {e} -- logging disabled for this session.")
            self._broken = True

    def log_detection(self, camera_id, class_id, conf, bbox, size, avg_size=(0.0, 0.0), track_id=-1, frame_id=-1, track_age=0):
        """Record a detection in the CSV. A silent no-op if the logger is in
        "broken" mode (see __init__ or an earlier write error)."""
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
            # flush() only pushes the data to the system cache, not physically
            # onto the SD card -- fsync() guarantees that. Isolated site with no
            # battery backup: a brutal power cut could otherwise lose the last
            # detections despite the flush().
            os.fsync(self.file.fileno())
        except OSError as e:
            logger.error(f"[CSVLogger] Write failed ({self.path}): {e} -- logging disabled for this session.")
            self._broken = True

    def close(self):
        # Idempotent: a close() called twice (e.g. a future regression that
        # forgets to check the state before recreating a logger) does nothing the
        # second time, instead of risking a descriptor leak if the first call had
        # been skipped by mistake elsewhere in the code.
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
            logger.error(f"[PerformanceLogger] Init failed ({self.path}): {e} -- logging disabled for this session.")
            self._broken = True

    def log_stats(self, cam_fps, inference_ms, cpu_temp, cpu_load, ram_usage):
        """A silent no-op if the logger is in "broken" mode (disk full or
        read-only FS detected at init or on an earlier write) -- the agent
        (camera, AI, upload) keeps running even if these logs are lost."""
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
                # Same reasoning as CSVLogger: flush() alone does not guarantee
                # a physical write to the SD card before a power cut.
                os.fsync(f.fileno())
        except OSError as e:
            logger.error(f"[PerformanceLogger] Write failed ({self.path}): {e} -- logging disabled for this session.")
            self._broken = True
        return self.path

    def close(self):
        # Idempotent for consistency with CSVLogger, even though the file is
        # already opened and closed on every write (nothing to do here today, but
        # a future refactor to a persistent descriptor would inherit this guard).
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
