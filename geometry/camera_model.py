"""Camera model computations for projective geometry."""
import numpy as np

class Camera:
    def __init__(self, resolution_px, FOVx_rad=None, FOVy_rad=None, 
                    sensor_size=None, focal_length=None, position=None,
                    yaw_rad=0.0, pitch_rad=0.0):
        self.resolution_px = resolution_px
        self.position = np.zeros(3) if position is None else np.array(position)

        # Internal FOV in radians
        self._FOVx_rad = FOVx_rad
        self._FOVy_rad = FOVy_rad

        # Intrinsic parameters (optional)
        self._sensor_size = sensor_size
        self._focal_length = focal_length

        self._yaw_rad = yaw_rad
        self._pitch_rad = pitch_rad

    def sensor_and_focal_have_been_set(self):
        """Check if sensor size and focal length are set."""
        return self._sensor_size is not None and self._focal_length is not None

    # -----------------------------
    # Flexible setters/getters
    # -----------------------------
    @property
    def FOVx_rad(self):
        return self._FOVx_rad

    @FOVx_rad.setter
    def FOVx_rad(self, value):
        self._FOVx_rad = value

    @property
    def FOVy_rad(self):
        return self._FOVy_rad

    @FOVy_rad.setter
    def FOVy_rad(self, value):
        self._FOVy_rad = value

    @property
    def FOVx_deg(self):
        return np.degrees(self._FOVx_rad)

    @FOVx_deg.setter
    def FOVx_deg(self, value):
        self._FOVx_rad = np.radians(value)

    @property
    def FOVy_deg(self):
        return np.degrees(self._FOVy_rad)

    @FOVy_deg.setter
    def FOVy_deg(self, value):
        self._FOVy_rad = np.radians(value)

    # If use FOV, assume it's FOVx and update FOVy to match the aspect ratio
    @property
    def FOV_rad(self):
        return self._FOVx_rad

    @FOV_rad.setter
    def FOV_rad(self, value):
        self._FOVx_rad = value
        self._FOVy_rad = value * self.resolution_px[1] / self.resolution_px[0]

    @property
    def FOV_deg(self):
        return np.degrees(self.FOV_rad)

    @FOV_deg.setter
    def FOV_deg(self, value):
        self.FOV_rad = np.radians(value)
    

    @property
    def yaw_rad(self):
        return self._yaw_rad
    @yaw_rad.setter
    def yaw_rad(self, value):
        self._yaw_rad = value

    @property
    def pitch_rad(self):
        return self._pitch_rad
    @pitch_rad.setter
    def pitch_rad(self, value):
        self._pitch_rad = value

    @property
    def yaw_deg(self):
        return np.degrees(self._yaw_rad)
    @yaw_deg.setter
    def yaw_deg(self, value):
        self._yaw_rad = np.radians(value)

    @property
    def pitch_deg(self):
        return np.degrees(self._pitch_rad)
    @pitch_deg.setter
    def pitch_deg(self, value):
        self._pitch_rad = np.radians(value)


    # -----------------------------
    # Derived properties
    # -----------------------------

    @property
    def principal_point(self):
        return (self.resolution_px[0] - 1) / 2, (self.resolution_px[1] - 1) / 2


    # Focal lengths in pixels
    @property
    def fx(self):
        return self.resolution_px[0] / (2 * np.tan(self._FOVx_rad / 2))
    @property
    def fy(self):
        return self.resolution_px[1] / (2 * np.tan(self._FOVy_rad / 2))
    
    # Intrinsic matrix K
    @property
    def intrinsic_matrix(self):
        cx, cy = self.principal_point
        return np.array([[self.fx, 0,     cx],
                        [0,     self.fy, cy],
                        [0,     0,     1]])

    # Rotation matrix from camera to world coordinates
    @property
    def rotation_matrix(self):
        """Rotation matrix from camera to world coordinates (yaw around Y, pitch around X)."""
        cy = np.cos(self._yaw_rad)
        sy = np.sin(self._yaw_rad)
        cp = np.cos(self._pitch_rad)
        sp = np.sin(self._pitch_rad)

        # Yaw around Y-axis
        R_yaw = np.array([[ cy, 0, sy],
                        [  0, 1,  0],
                        [-sy, 0, cy]])

        # Pitch around X-axis
        R_pitch = np.array([[1,   0,    0],
                            [0,  cp, -sp],
                            [0,  sp,  cp]])

        return R_yaw @ R_pitch


    # -----------------------------
    # Intrinsics (read-only externally)
    # -----------------------------
    @property
    def sensor_size(self):
        if self._sensor_size is None:
            raise ValueError("Sensor size not set.")
        return self._sensor_size

    @property
    def focal_length(self):
        if self._focal_length is None:
            raise ValueError("Focal length not set.")
        return self._focal_length

    def set_focal_length_and_sensor(self, focal_length, sensor_size):
        """Set focal length and sensor size, and update FOV accordingly."""
        self._focal_length = focal_length
        self._sensor_size = sensor_size

        sensor_w, sensor_h = sensor_size
        self._FOVx_rad = 2 * np.arctan(sensor_w / (2 * focal_length))
        self._FOVy_rad = 2 * np.arctan(sensor_h / (2 * focal_length))
        
    def get_local_ray_direction_from_pixel(self, pixel_x, pixel_y):
        """Return normalized direction vector from camera through pixel without rotation."""
        if self._FOVx_rad is None or self._FOVy_rad is None:
            raise ValueError("FOVx and FOVy must be set before computing rays.")

        cx, cy = self.principal_point
        dx = (pixel_x - cx) / self.fx
        dy = (cy - pixel_y) / self.fy  # y flipped for standard convention
        dz = -1.0
        local_dir = np.array([dx, dy, dz])
        return local_dir / np.linalg.norm(local_dir)
        
    def get_world_ray(self, pixel_x, pixel_y):
        """Return ray origin and direction vector from camera through pixel."""
        local_dir = self.get_local_ray_direction_from_pixel(pixel_x, pixel_y)
        world_dir = self.rotation_matrix @ local_dir
        return self.position, world_dir / np.linalg.norm(world_dir)

    def get_camera_rays(self):
        """Return (positions, directions) for all screen pixels in world space."""
        w, h = self.resolution_px
        x_ndc = np.linspace(-1, 1, w)
        y_ndc = np.linspace(-1, 1, h)
        screen_x, screen_y = np.meshgrid(x_ndc, y_ndc)

        x = screen_x * np.tan(self._FOVx_rad / 2)
        y = -screen_y * np.tan(self._FOVy_rad / 2)
        z = -np.ones_like(x)

        dirs_local = np.stack((x, y, z), axis=-1)  # (H, W, 3)
        norms = np.linalg.norm(dirs_local, axis=2, keepdims=True)
        dirs_local /= norms

        # Rotate all directions to world frame
        R = self.rotation_matrix
        h, w = dirs_local.shape[:2]
        dirs_flat = dirs_local.reshape(-1, 3).T
        dirs_rotated = R @ dirs_flat
        dirs_world = dirs_rotated.T.reshape(h, w, 3)

        pos = np.broadcast_to(self.position, dirs_world.shape)
        return pos, dirs_world



# Example usage
if __name__ == "__main__":
    cam = Camera((1920, 1080))
    cam.FOV_deg = 90
    print("FOVx deg:", cam.FOVx_deg)
    print("FOVy deg:", cam.FOVy_deg)
    print("Ray center:", cam.get_world_ray(960, 540))
    print("FOVx rad:", cam.FOVx_rad)
    print("FOVy rad:", cam.FOVy_rad)

    cam.set_focal_length_and_sensor(4.6, (5.6, 3.2))
    print("FOVx deg (from optics):", cam.FOVx_deg)
    print("Ray center:", cam.get_world_ray(960, 540))
