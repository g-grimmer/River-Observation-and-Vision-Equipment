"""Geometric intersection calculations (ray-plane intersections, plane definitions)."""
import numpy as np
from .plane import Plane
from .utils import get_box_vertices_2D

def ray_plane_intersection(origin, direction, plane: Plane):
    """
    Intersect a ray with a given Plane.

    Parameters:
    - origin (np.ndarray): Ray origin point
    - direction (np.ndarray): Ray direction vector (not necessarily normalized)
    - plane (Plane): Plane object

    Returns:
    - np.ndarray or None: Intersection point, or None if no valid intersection
    """
    denom = np.dot(plane.normal, direction)
    if np.isclose(denom, 0) or denom >= 0:
        return None  # Ray is parallel or pointing away from the plane

    t = -(np.dot(plane.normal, origin) + plane.offset) / denom
    if t < 0:
        # Intersection is behind the ray origin
        return None
    return origin + t * direction

def ray_plane_intersection_y0(origin, direction):
    """
    Special-case intersection with horizontal plane y=0.
    Returns None if the ray is not pointing downward.
    """
    ground_plane = Plane(normal=[0, 1, 0], point_on_plane=[0, 0, 0])
    return ray_plane_intersection(origin, direction, ground_plane)

def get_plane_from_rays(ray_dir_1, ray_dir_2, camera_pos):
    """
    Get the plane defined by two rays originating from the camera position.

    Parameters:
    - ray1, ray2 (np.ndarray): Ray direction vectors
    - camera_pos (np.ndarray): Origin point (camera position)

    Returns:
    - Plane or None: Plane object, or None if rays are colinear
    """
    # Normalize the ray directions
    v1 = ray_dir_1 / np.linalg.norm(ray_dir_1)
    v2 = ray_dir_2 / np.linalg.norm(ray_dir_2)

    # Compute normal from cross product
    normal = np.cross(v1, v2)
    norm_len = np.linalg.norm(normal)
    if norm_len == 0:
        return None  # Rays are colinear, no valid plane

    normal = normal / norm_len
    return Plane(normal=normal, point_on_plane=camera_pos)

def get_projected_box_vertices(camera, rect_x, rect_y, rect_width, rect_height):

    vertices_2D = get_box_vertices_2D(rect_x, rect_y, rect_width, rect_height)

    vertices_3D = {}
    for key, (sx, sy) in vertices_2D.items():
        world_ray_pos, world_ray_dir = camera.get_world_ray(sx, sy)
        hit = ray_plane_intersection_y0(world_ray_pos, world_ray_dir)
        if hit is not None:
            vertices_3D[key] = hit
        else:
            vertices_3D[key] = np.array([np.nan, np.nan, np.nan])

    return vertices_3D

# Get the 4 planes formed by the box edges and the camera position.
def get_box_planes(camera, rect_x, rect_y, rect_width, rect_height):
    box_vertices_2D = get_box_vertices_2D(rect_x, rect_y, rect_width, rect_height)

    corner_rays = {}
    for key, (sx, sy) in box_vertices_2D.items():
        _, world_ray_dir = camera.get_world_ray(sx, sy)
        corner_rays[key] = world_ray_dir

    ray_pairs = {
        'top': (corner_rays['top_left'], corner_rays['top_right']),
        'right': (corner_rays['top_right'], corner_rays['bottom_right']),
        'bottom': (corner_rays['bottom_right'], corner_rays['bottom_left']),
        'left': (corner_rays['bottom_left'], corner_rays['top_left'])
    }

    # Get the planes from the ray pairs (edges)
    planes = {}
    for key, (ray1, ray2) in ray_pairs.items():
        plane = get_plane_from_rays(ray1, ray2, camera.position)
        if plane is not None:
            planes[key] = plane

    return planes

# Get the distance from a point and each plane.
def get_point_distances_to_planes(point, planes):
    """
    Get signed distances from a point to each plane.

    Parameters:
    - point (np.ndarray): 3D point to measure distances from
    - planes (list of Plane): List of Plane objects

    Returns:
    - dict: Distances keyed by plane index
    """
    distances = {}
    for key, plane in planes.items():
        distances[key] = abs(plane.signed_distance_to(point))
    return distances

def get_largest_diameters(camera, rect_x, rect_y, rect_width, rect_height):
    """
    Compute the largest fitting ellipse diameters for a rectangle in camera view.

    Parameters:
    - camera: Camera object with position and ray computation methods
    - rect_x, rect_y: Top-left corner of the rectangle in pixel coordinates
    - rect_width, rect_height: Width and height of the rectangle in pixels

    Returns:
    - tuple: (largest_x_diameter, largest_y_diameter), ou (nan, nan) si la
      géométrie est dégénérée (bbox de largeur ou hauteur nulle -> rayons de
      coin colinéaires -> get_box_planes ne peut pas produire les 4 plans).
    """
    planes = get_box_planes(camera, rect_x, rect_y, rect_width, rect_height)

    # Bbox dégénérée (largeur/hauteur nulle, coin dupliqué, etc.) : get_box_planes
    # peut ne pas contenir les 4 clés attendues. Avant, l'accès direct plus bas
    # (distances['top'], ...) levait un KeyError non géré qui remontait jusqu'à
    # planter tout l'agent. On ne peut simplement pas mesurer cet objet : NaN,
    # à charge pour l'appelant (voir detection/tracker.py) de l'exclure proprement.
    if not all(k in planes for k in ('top', 'bottom', 'left', 'right')):
        return float('nan'), float('nan')

    # Get center point in 3D
    center_projected_3D = get_projected_box_vertices(camera, rect_x, rect_y, rect_width, rect_height)['center']
    
    # Get distances from center to each plane
    distances = get_point_distances_to_planes(center_projected_3D, planes)

    # Extract diameters
    largest_x = distances['right'] + distances['left']
    largest_y = distances['top'] + distances['bottom']

    # # left=right and top=bottom if the rectangle is at the center of the camera view, different otherwise
    # print(f'left: {distances["left"]:.3f}, right: {distances["right"]:.3f}, top: {distances["top"]:.3f}, bottom: {distances["bottom"]:.3f}')
    
    return largest_x, largest_y
