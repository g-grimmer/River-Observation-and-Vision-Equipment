from ultralytics import YOLO
from utils.configs import TRACKER_CONFIG_PATH

# Importing this module registers the `river` tracker type with Ultralytics.
from detection import river_tracker  # noqa: F401

class YoloDetector:
    def __init__(self, model_path, conf, imgsz=None, tracker=None):
        self.model = YOLO(model_path)
        self.conf = conf
        # The tracking settings depend on the regime: see TRACKER_CONFIG_DAY/NIGHT.
        self.tracker = tracker or TRACKER_CONFIG_PATH
        # NCNN exports are frozen at their training resolution (960). Without an
        # explicit imgsz, Ultralytics falls back to its own 640 default and the
        # model runs outside the resolution it was trained for -- a loss of
        # recall on small objects, which are here almost all of the targets.
        self.imgsz = imgsz

    def predict(self, frame):
        # TRACK is used rather than PREDICT (ByteTrack by default, or the one given).
        # persist=True matters: it is what keeps identities alive between frames.
        # TRACKER_CONFIG_PATH is anchored on BASE_DIR (utils/configs.py): a relative
        # path here depended on the process working directory rather than on the
        # repository location -- it failed with a FileNotFoundError whenever the
        # agent was launched by hand from another directory.
        kw = dict(conf=self.conf, persist=True,
                  tracker=self.tracker, verbose=False)
        if self.imgsz:
            kw["imgsz"] = self.imgsz
        return self.model.track(frame, **kw)
