import threading
import time
from queue import Queue

from utils.configs import CAPTURE_FPS
try:
    from picamera2 import Picamera2
except ImportError:
    print("⚠️ Picamera2 not found. Using Mock Camera.")
    import numpy as np
    class Picamera2:
        def __init__(self, cam_id):
            self.cam_id = cam_id
        def create_preview_configuration(self, main):
            return main
        def configure(self, config):
            pass
        def start(self):
            print(f"📷 [Mock] Camera {self.cam_id} started")
        def stop(self):
            print(f"📷 [Mock] Camera {self.cam_id} stopped")
        def close(self):
            pass
        def capture_array(self, stream_name):
            size = (640, 640) if stream_name == "lores" else (4056, 3040)
            img = np.zeros((size[1], size[0], 3), dtype=np.uint8)
            cv2.putText(img, f"MOCK CAM {self.cam_id} {stream_name}", (50, size[1]//2), 
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            cv2.putText(img, f"{time.time():.1f}", (50, size[1]//2 + 50), 
                        cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
            return img
            
        def capture_file(self, filepath, options=None):
            import cv2
            img = self.capture_array("main")
            cv2.imwrite(filepath, img)
    
    # We need cv2 for the mock to draw text, ensure it's imported
    import cv2

class CameraThread(threading.Thread):
    def __init__(self, cam_id, frame_size, hi_res_size=(4056, 3040), queue_size=2):
        super().__init__(daemon=True)

        self.cam_id = cam_id
        self.queue = Queue(maxsize=queue_size)
        self.frame_size = frame_size
        self.hi_res_size = hi_res_size
        self.running = True
        self.is_streaming = False # State flag
        self.actual_fps = 0.0

        print(f"📷 Init Camera {cam_id}...")
        self.picam = Picamera2(cam_id)
        config = self.picam.create_preview_configuration(
            main={"size": hi_res_size, "format": "RGB888"},
            lores={"size": frame_size, "format": "RGB888"}
        )
        self.picam.configure(config)

        # Note: We do NOT start() here automatically anymore.
        
    def enable(self):
        """Start streaming (allocate buffers/start sensor).

        L'échec de picam.start() est relogué puis RE-LEVÉ : avant, il était avalé
        ici, ce qui empêchait le try/except protecteur de main.py::select_camera()
        de jamais voir un échec de démarrage (seul un échec de construction était
        visible). is_streaming ne passe à True que si start() a réellement réussi.
        """
        if not self.is_streaming:
            print(f"🟢 Activating Camera {self.cam_id}")
            try:
                self.picam.start()
            except Exception as e:
                print(f"❌ Error starting camera {self.cam_id}: {e}")
                raise
            self.is_streaming = True

    def disable(self):
        """Stop streaming (release bandwidth, keep object alive)"""
        if self.is_streaming:
            print(f"🔴 Pausing Camera {self.cam_id}")
            try:
                self.picam.stop()
                self.is_streaming = False
                self.actual_fps = 0.0
            except Exception as e:
                print(f"⚠️ Error stopping camera {self.cam_id}: {e}")

    def capture_high_res_image(self, save_path):
        """ Capture une image haute résolution via l'ISP matériel.
        Le format JPEG est inféré automatiquement depuis l'extension .jpg.
        """
        try:
            if self.is_streaming:
                self.picam.capture_file(save_path)  # format inféré depuis l'extension
                return True
        except Exception as e:
            print(f"⚠️ Erreur capture hi-res: {e}")
        return False

    def run(self):
        t_start = time.time()
        frame_count = 0
        period = 1.0 / CAPTURE_FPS
        next_t = time.monotonic()

        while self.running:
            if self.is_streaming:
                try:
                    # Capture un flux basse résolution pour l'IA sans bloquer le main (Goulot I/O fix)
                    frame = self.picam.capture_array("lores")
                    
                    # Cadence alignée sur CAPTURE_FPS (= VIDEO_FPS = 3 img/s).
                    # On vise une échéance plutôt qu'un sleep fixe : avec un sleep
                    # constant, le temps de capture s'ajoute à la période et la
                    # cadence réelle dérive sous la consigne.
                    next_t += period
                    delay = next_t - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        # Retard pris (capture lente) : on repart de maintenant
                        # plutôt que d'accumuler une dette et de rafaler ensuite.
                        next_t = time.monotonic()
                    
                    # FPS Calculation
                    frame_count += 1
                    now = time.time()
                    if now - t_start >= 1.0:
                        self.actual_fps = frame_count / (now - t_start)
                        frame_count = 0
                        t_start = now

                    if self.queue.full():
                        # File pleine : on jette la plus ANCIENNE plutôt que de
                        # perdre la plus récente. Pour du débris qui dérive vite
                        # dans le courant, mieux vaut analyser l'image la plus
                        # fraîche possible qu'une frame déjà périmée.
                        try:
                            self.queue.get_nowait()
                        except Exception:
                            pass
                    self.queue.put(frame)
                except Exception as e:
                    # If we stopped right during capture, ignore error
                    if self.is_streaming:
                        print(f"⚠️ Capture error {self.cam_id}: {e}")
                    time.sleep(0.1)
            else:
                # Idle loop
                time.sleep(0.1)

    def read(self):
        if not self.queue.empty():
            return self.queue.get()
        return None

    def stop(self):
        """Final cleanup"""
        self.running = False
        self.disable()
        time.sleep(0.1)
        try:
            self.picam.close()
            print(f"💀 Camera {self.cam_id} closed.")
        except Exception:
            pass
