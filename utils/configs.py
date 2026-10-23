import datetime
import os
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- Camera Configuration ---
# HI_RES_SIZE is for Video Recording (Compromise for 12MP Module V3)
# 1280x720 (720p) is lighter for storage and 4G uploads.
HI_RES_SIZE = (1280, 720)

# FRAME_SIZE is for YOLO Inference (Standard)
# Must preserve the sensor aspect ratio (2304x1296 = 16:9). A square format
# applied two different scale factors -- 0.417 in x, 0.741 in y -- and stretched
# everything by 1.8x in height: a square 30x28 px debris item became a 12x21
# rectangle. The models having been trained on letterboxed images (ratio
# preserved), in production they saw shapes they had never learnt. Measured:
# AP@0.5 0.900 -> 0.926 by day and 0.782 -> 0.821 at night. Ultralytics does the
# letterboxing to 960x960 itself, so inference costs exactly the same.
FRAME_SIZE = (960, 540)

# Recording Settings
VIDEO_FPS = 3.0

# Acquisition rate of the camera thread, aligned on VIDEO_FPS.
# Capturing faster is pointless: the analysis loop does not consume more and the
# surplus was discarded by the bounded queue -- 33 % of the frames produced,
# measured with the pipeline harness. The CPU thus freed benefits inference.
CAPTURE_FPS = VIDEO_FPS
VIDEO_CODEC = "mp4v" # or "XVID"
VIDEO_EXTENSION = "mp4"

# Two cameras CSI Raspberry Pi
# 0: RGB  (diurnal regime, see HEURE_JOUR_DEBUT/FIN)
# 1: NoIR (nocturnal regime)
CAMERA_IDS = [0, 1]

# --- Diurnal regime time window ---------------------------------------------
# The diurnal regime runs from HEURE_JOUR_DEBUT (inclusive) to HEURE_JOUR_FIN
# (exclusive); the rest of the day is nocturnal. Expressed this way, and not as
# a single threshold, because a lone `hour < 20` left the midnight-to-20:00 slot
# in the diurnal regime: from midnight to 08:00 in winter the instrument was
# observing in the dark with the RGB camera, the day model and the day tracking
# settings.
HEURE_JOUR_DEBUT = 7   # 07:00 -> camera 0 (RGB) + diurnal model
HEURE_JOUR_FIN   = 19  # 19:00 -> camera 1 (NoIR) + nocturnal model


def est_jour(moment=None) -> bool:
    """True if `moment` (datetime, defaults to now) falls in the diurnal regime.

    Single source of the day/night decision: it used to be written twice in
    main.py (once at start-up, once in select_camera), with the risk that a
    change would fix only one of them and that the loaded model would no longer
    match the selected camera.
    """
    heure = (moment or datetime.datetime.now()).hour
    return HEURE_JOUR_DEBUT <= heure < HEURE_JOUR_FIN

# --- AI Configuration ---
# Automatic day/night switch (see est_jour above): camera 0 (RGB, 07:00 to
# 19:00) -> diurnal model, camera 1 (NoIR, 19:00 to 07:00) -> nocturnal model.
# MODEL_PATH stays the default model, used when no switch is relevant (e.g. the
# very first load before camera selection) or when --model is passed explicitly
# on the command line (in which case the automatic switch is disabled).
# Anchored on BASE_DIR (not relative to the process working directory): a manual
# launch from another directory (remote SSH intervention) must not break the
# model load with a FileNotFoundError.
# Single-class YOLO26n 960x960, NCNN exports (05_RESULTS/04_NCNN).
# NCNN and not .pt: measured at 157 ms against 460 ms for the PyTorch 26s.
MODEL_PATH_DAY = os.path.join(BASE_DIR, "models_yolo26n", "yolo26n_day_best_ncnn_model")
MODEL_PATH_NIGHT = os.path.join(BASE_DIR, "models_yolo26n", "yolo26n_night_best_ncnn_model")
MODEL_PATH = MODEL_PATH_DAY
CONFIDENCE = 0.10

# Inference resolution, passed to Ultralytics by YoloDetector.
IMGSZ = 960

# --- Camera pose ------------------------------------------------------------
# Centralised here because it used to be duplicated in main.py and in the
# evaluation harness, with diverging values. The estimated size is strictly
# linear in CAMERA_HAUTEUR_M: two different values give two incomparable sets of
# measurements.
#
# WARNING: these values come from the "Fixed for test" comment in main.py.
# CAMERA_HAUTEUR_M must be surveyed above the STAFF GAUGE ZERO, and not above
# the water on the day of installation, otherwise the hydrological correction
# has no reference datum.
# Measured sensitivity: 1 deg of tilt = 3.7 % of size; 10 cm of height = 3.7 %.
CAMERA_HAUTEUR_M = 2.7
CAMERA_TANGAGE_DEG = -19.0
CAMERA_FOCALE_MM = 4.74
CAMERA_CAPTEUR_MM = (6.45, 3.63)

# Maximum range beyond which the size estimate is rejected.
# Set it to the distance of the far bank: further out there is no water left.
# Below the horizon the rays become grazing and the estimate diverges -- at the
# current pose the range is 69 m at y_c = 0.10 against 4 m at the bottom of the
# image. 20 m corresponds here to y_c ~ 0.22; adjust after surveying the site.
PORTEE_MAX_M = 20.0

# Tracking configuration, anchored on BASE_DIR for the same reason.
# Two files: the measured inter-frame displacement is 1.38 times the object size
# by day against 0.31 at night, and the optimal setting depends on it
# (max_move_ratio 6.0 by day, 4.0 at night).
TRACKER_CONFIG_DAY = os.path.join(BASE_DIR, "river_day.yaml")
TRACKER_CONFIG_NIGHT = os.path.join(BASE_DIR, "river_night.yaml")
TRACKER_CONFIG_PATH = TRACKER_CONFIG_DAY   # default, before the first switch

# Rotation Settings
ROTATION_MINUTES = 5  # Duration of each video sequence (minutes)

# --- Local Storage ---
VIDEO_DIR = os.path.join(BASE_DIR, "videos")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# Ensure dirs exist
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# --- SFTP Configuration ---
# Intentionally blank in the repository. These four values are filled in at
# installation time by setup_agent.sh (section "NAS / SFTP Configuration"),
# which rewrites them here. Leaving them in clear text in the code meant
# publishing them in the git history, from which they cannot be removed
# afterwards.
# An empty host effectively disables transfers: SFTPManager says so at start-up
# and the files stay on the SD card instead of leaving.
SFTP_HOST = ""
SFTP_PORT = 22
SFTP_USERNAME = ""
SFTP_PASSWORD = ""
SFTP_REMOTE_DIR = ""
