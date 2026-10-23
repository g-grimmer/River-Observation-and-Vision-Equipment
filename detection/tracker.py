import time
import math
import cv2
from geometry.size_estimator import estimate_size_m
from utils.configs import PORTEE_MAX_M

class ObjectTracker:
    def __init__(self, logger):
        self.logger = logger
        
        # Tracking State
        # Structure: {id: {'dims': [], 'last_seen': time, 'cls': cls, 'conf': conf, 'box': box, 'cam_id': cam_id}}
        self.active_tracks = {}
        
        self.class_counts = {}
        self.recent_detections = [] # For dashboard
        
    def update(self, res, cam_id, camera_model, log_every_frame=True, frame_id=-1, log_all_occurrences=False):
        """
        Process a Yolo result:
        - Update tracks
        - Calculate dimensions
        - Annotate frame
        - Returns: annotated_frame, new_track_ids (list)
        """
        annotated = res.plot()
        now = time.time()
        new_track_ids = []
        
        orig_h, orig_w = res.orig_shape[:2]
        
        for box in res.boxes:
            x1, y1, x2, y2 = box.xyxy[0]
            cls = int(box.cls[0])
            conf = float(box.conf[0])
            
            # Relative coordinates
            x1_rel = float(x1) / orig_w
            y1_rel = float(y1) / orig_h
            x2_rel = float(x2) / orig_w
            y2_rel = float(y2) / orig_h
            rel_box = (x1_rel, y1_rel, x2_rel, y2_rel)
            
            # ByteTrack ID
            track_id = int(box.id[0]) if box.id is not None else -1

            # Geometric Estimation -- a ray that does not meet the valid
            # measurement plane (object above the horizon, degenerate bbox)
            # returns NaN. That NaN is kept as such (no -1.0 sentinel):
            # pandas/numpy automatically exclude NaN from .mean()/.std(),
            # whereas a numeric value such as -1.0 would be treated as a real
            # measurement and would silently distort the CSV statistics. The only
            # place where a raw NaN would be a problem is the rolling average
            # below (Python sum/division), handled separately.
            w_m, h_m = estimate_size_m(camera_model, float(x1), float(y1),
                                       float(x2), float(y2),
                                       portee_max_m=PORTEE_MAX_M)
            geometry_valid = not (math.isnan(w_m) or math.isnan(h_m))
            avg_w, avg_h = w_m, h_m
            track_age = 0

            # Update Active Tracks
            if track_id != -1:
                if track_id not in self.active_tracks:
                    # New Track Analysis
                    self.active_tracks[track_id] = {
                        'dims': [], 'last_seen': now,
                        'cls': cls, 'conf': conf,
                        'box': (x1, y1, x2, y2), 'cam_id': cam_id,
                        'first_sight_captured': False,
                        'logged': False,
                        'frames_seen': 0,
                    }
                    new_track_ids.append(track_id)

                    # Add to recent detections for Dashboard (First Sight log).
                    # None (not NaN) to stay compatible with a future
                    # json.dumps(): a literal NaN would be accepted by Python's
                    # standard json (which is not JSON-spec compliant) but
                    # rejected by most third-party JSON parsers (JS, etc.).
                    self.recent_detections.append({
                        "time": time.strftime("%H:%M:%S", time.localtime(now)),
                        "class": cls,
                        "width": None if math.isnan(w_m) else w_m,
                        "height": None if math.isnan(h_m) else h_m,
                        "conf": conf,
                        "id": track_id
                    })
                    if len(self.recent_detections) > 50:
                        self.recent_detections.pop(0)

                    self.class_counts[cls] = self.class_counts.get(cls, 0) + 1

                # Update existing track
                track = self.active_tracks[track_id]
                track['last_seen'] = now
                track['box'] = (x1, y1, x2, y2)
                track['conf'] = max(track['conf'], conf)
                # Incremented on EVERY frame where this track exists, regardless
                # of geometric validity -- it reflects the real track duration.
                # (len(track['dims']) only counts valid measurements: a track
                # permanently outside the measurement zone would stay stuck at 0.)
                track['frames_seen'] += 1

                # Accumulate dimensions for averaging -- invalid (NaN) samples are
                # excluded from the rolling window, otherwise a single NaN sample
                # makes the whole average NaN for ~3 s (the window length).
                if geometry_valid:
                    track['dims'].append((w_m, h_m))
                    if len(track['dims']) > 90: # Keep ~3s history
                        track['dims'].pop(0)

                # Compute Average -- NaN as long as no valid measurement has been
                # accumulated for this track yet (consistent with the CSV: pandas
                # will exclude it automatically from aggregations).
                if track['dims']:
                    avg_w = sum(d[0] for d in track['dims']) / len(track['dims'])
                    avg_h = sum(d[1] for d in track['dims']) / len(track['dims'])
                else:
                    avg_w, avg_h = float('nan'), float('nan')
                track_age = track['frames_seen']

            # Logging logic
            if self.logger:
                if log_all_occurrences:
                    # Log EVERYTHING in bench mode, including track_id = -1
                    self.logger.log_detection(
                        cam_id, cls, conf,
                        rel_box, (w_m, h_m), (avg_w, avg_h), track_id=track_id, frame_id=frame_id, track_age=track_age
                    )
                elif log_every_frame and track_id != -1:
                    # Normal mode: log only ONCE per track_id, and only from the
                    # first frame on which the geometry is valid -- otherwise the
                    # single CSV row of that track could stay frozen on a bogus
                    # NaN/sentinel forever, even if every later measurement of the
                    # same track was perfectly valid (never caught up, since the
                    # log happens only once).
                    track = self.active_tracks[track_id]
                    if not track.get('logged', False) and geometry_valid:
                        self.logger.log_detection(
                            cam_id, cls, conf,
                            rel_box, (w_m, h_m), (avg_w, avg_h), track_id=track_id, frame_id=frame_id, track_age=track_age
                        )
                        track['logged'] = True

            # Draw size & ID -- explicit text rather than a number that would
            # imitate a real measurement (a raw NaN would be just as misleading).
            if track_id != -1:
                if math.isnan(avg_w) or math.isnan(avg_h):
                    label = f"ID:{track_id} | outside measurement zone"
                else:
                    label = f"ID:{track_id} | {avg_w:.2f}m x {avg_h:.2f}m"
            else:
                label = "outside measurement zone" if (math.isnan(w_m) or math.isnan(h_m)) else f"{w_m:.2f}m x {h_m:.2f}m"
            cv2.putText(annotated, label, (int(x1), int(y1)-5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 2)
                         
        return annotated, new_track_ids

    def check_lost_tracks(self, timeout=2.0):
        """
        Check for tracks that haven't been seen for > timeout seconds.
        Removes from active tracks.
        """
        now = time.time()
        expired_ids = []
        
        for tid, data in self.active_tracks.items():
            if now - data['last_seen'] > timeout:
                expired_ids.append(tid)
        
        # Remove expired
        for tid in expired_ids:
            self.active_tracks.pop(tid, None)


            
    def get_stats(self):
        """Return a snapshot of stats for the dashboard"""
        return {
            "class_counts": self.class_counts.copy(),
            "recent_detections": list(self.recent_detections)
        }
