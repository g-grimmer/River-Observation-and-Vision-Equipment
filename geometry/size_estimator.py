import math

from .intersection import get_largest_diameters


def estimate_size_m(camera, x1, y1, x2, y2, portee_max_m=None):
    """Physical size of the object in metres, from the YOLO box.

    Returns (nan, nan) whenever the box cannot be exploited geometrically.
    `tracker.py` already tests for this case (`geometry_valid`) and excludes NaN
    from the averages: an absurd but finite value would instead enter the
    statistics silently.

    Two safeguards, both applied to the TOP edge of the box, which yields the
    most grazing ray:

    1. Above the horizon the ray never meets the water plane. With the declared
       pose (H=2.7 m, tilt -19 deg, vertical half-field 20.95 deg) the top of the
       image points 1.95 deg above the horizontal: the upper 5 % of the image
       falls in this case.

    2. Just below the horizon the ray is grazing and its intersection runs far
       away: at y_c = 0.10 the range is 69 m and a single pixel of box displaces
       the estimate by 5.3 cm, against 0.33 cm at the bottom of the image.
       `portee_max_m` should be set to the distance of the far bank: beyond it
       there is no water left, hence no debris to measure.

    Returns: (width_m, height_m)
    """
    _, direction = camera.get_world_ray((x1 + x2) / 2.0, y1)

    # get_world_ray returns a unit vector; the water plane is y = 0 and the
    # camera sits at y = H (see intersection.ray_plane_intersection_y0).
    if direction[1] >= 0:
        return float("nan"), float("nan")

    if portee_max_m is not None:
        portee = camera.position[1] / (-direction[1])
        if portee > portee_max_m:
            return float("nan"), float("nan")

    return get_largest_diameters(camera, x1, y1, x2 - x1, y2 - y1)
