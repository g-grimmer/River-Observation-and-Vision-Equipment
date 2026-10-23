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

        A failure of picam.start() is logged and then RE-RAISED: it used to be
        swallowed here, which prevented the protective try/except in
        main.py::select_camera() from ever seeing a start-up failure (only a
        construction failure was visible). is_streaming only becomes True if
        start() genuinely succeeded.
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
        """ Capture a high-resolution image through the hardware ISP.
        The JPEG format is inferred automatically from the .jpg extension.
        """
        try:
            if self.is_streaming:
                self.picam.capture_file(save_path)  # format inferred from the extension
                return True
        except Exception as e:
            print(f"⚠️ Hi-res capture error: {e}")
        return False

    def run(self):
        t_start = time.time()
        frame_count = 0
        period = 1.0 / CAPTURE_FPS
        next_t = time.monotonic()

        while self.running:
            if self.is_streaming:
                try:
                    # Grab a low-resolution frame for the AI without blocking main (I/O bottleneck fix)
                    frame = self.picam.capture_array("lores")
                    
                    # Rate aligned on CAPTURE_FPS (= VIDEO_FPS = 3 fps).
                    # A deadline is targeted rather than a fixed sleep: with a
                    # constant sleep the capture time adds to the period and the
                    # real rate drifts below the set point.
                    next_t += period
                    delay = next_t - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    else:
                        # Running late (slow capture): restart from now rather
                        # than accumulating a debt and then bursting.
                        next_t = time.monotonic()
                    
                    # FPS Calculation
                    frame_count += 1
                    now = time.time()
                    if now - t_start >= 1.0:
                        self.actual_fps = frame_count / (now - t_start)
                        frame_count = 0
                        t_start = now

                    if self.queue.full():
                        # Queue full: drop the OLDEST rather than lose the most
                        # recent one. For debris drifting fast in the current, it
                        # is better to analyse the freshest frame available than
                        # an already stale one.
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
