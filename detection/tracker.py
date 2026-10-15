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

            # Geometric Estimation — un rayon qui ne coupe pas le plan de mesure
            # valide (objet au-dessus de l'horizon, bbox dégénérée) renvoie NaN.
            # On garde ce NaN tel quel (pas de sentinelle -1.0) : pandas/numpy
            # excluent automatiquement un NaN de .mean()/.std(), alors qu'une
            # valeur numérique comme -1.0 serait traitée comme une vraie mesure
            # et fausserait silencieusement les statistiques du CSV. Le seul
            # endroit où un NaN brut serait un problème est la moyenne glissante
            # ci-dessous (somme/division Python), traité séparément.
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
                    # None (pas NaN) pour rester compatible avec un futur
                    # json.dumps() : un NaN littéral serait accepté par le json
                    # standard de Python (non conforme au spec JSON) mais
                    # rejeté par la plupart des parseurs JSON tiers (JS, etc.).
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
                # Incrémenté à CHAQUE frame où ce track existe, indépendamment de
                # la validité géométrique -- reflète la durée réelle du track.
                # (len(track['dims']) ne compte que les mesures valides : un track
                # en permanence hors zone de mesure resterait sinon bloqué à 0.)
                track['frames_seen'] += 1

                # Accumulate dimensions for averaging — on exclut les mesures
                # invalides (NaN) de la fenêtre glissante, sinon un seul échantillon
                # NaN rend toute la moyenne NaN pendant ~3s (durée de la fenêtre).
                if geometry_valid:
                    track['dims'].append((w_m, h_m))
                    if len(track['dims']) > 90: # Keep ~3s history
                        track['dims'].pop(0)

                # Compute Average — NaN tant qu'aucune mesure valide n'a encore
                # été accumulée pour ce track (cohérent avec le CSV : pandas
                # l'exclura automatiquement des agrégations).
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
                    # Normal mode: log only ONCE per track_id, et seulement à
                    # partir du premier frame où la géométrie est valide — sinon
                    # l'unique ligne CSV de ce track pouvait rester figée sur un
                    # NaN/sentinelle bidon pour toujours, même si toutes les
                    # mesures suivantes du même track étaient parfaitement
                    # valides (jamais rattrapées, puisque le log ne se produit
                    # qu'une fois).
                    track = self.active_tracks[track_id]
                    if not track.get('logged', False) and geometry_valid:
                        self.logger.log_detection(
                            cam_id, cls, conf,
                            rel_box, (w_m, h_m), (avg_w, avg_h), track_id=track_id, frame_id=frame_id, track_age=track_age
                        )
                        track['logged'] = True

            # Draw size & ID — texte explicite plutôt qu'un nombre qui imiterait
            # une vraie mesure (NaN affiché tel quel serait tout aussi trompeur).
            if track_id != -1:
                if math.isnan(avg_w) or math.isnan(avg_h):
                    label = f"ID:{track_id} | hors zone de mesure"
                else:
                    label = f"ID:{track_id} | {avg_w:.2f}m x {avg_h:.2f}m"
            else:
                label = "hors zone de mesure" if (math.isnan(w_m) or math.isnan(h_m)) else f"{w_m:.2f}m x {h_m:.2f}m"
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
