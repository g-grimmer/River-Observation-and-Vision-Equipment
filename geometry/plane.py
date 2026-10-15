"""Geometric Plane representation."""
import numpy as np

class Plane:
    def __init__(self, normal, point_on_plane):
        """
        Create a plane from a normal vector and a point on the plane.
        Ensures normal is normalized and offset is consistent.
        """
        self.normal = self._normalize(np.array(normal))
        self.offset = -np.dot(self.normal, np.array(point_on_plane))


    @classmethod
    def from_points(cls, p1, p2, p3):
        """Create a plane from three non-collinear points."""
        p1, p2, p3 = map(np.array, (p1, p2, p3))
        v1 = p2 - p1
        v2 = p3 - p1
        normal = np.cross(v1, v2)
        norm = np.linalg.norm(normal)
        if norm == 0:
            raise ValueError("Provided points are collinear.")
        return cls(normal / norm, p1)

    @classmethod
    def from_rays(cls, ray1, ray2, origin):
        """Create a plane from two ray directions and a common origin."""
        v1 = ray1 / np.linalg.norm(ray1)
        v2 = ray2 / np.linalg.norm(ray2)
        normal = np.cross(v1, v2)
        norm = np.linalg.norm(normal)
        if norm == 0:
            raise ValueError("Provided rays are collinear.")
        return cls(normal / norm, origin)

    def signed_distance_to(self, point):
        """Compute signed distance from a point to the plane."""
        point = np.array(point) # In case point is a list or tuple
        return np.dot(self.normal, point) + self.offset

    def project_point(self, point):
        """Project a point orthogonally onto the plane."""
        point = np.array(point) # In case point is a list or tuple
        d = self.signed_distance_to(point)
        return point - d * self.normal

    @staticmethod
    def _normalize(v):
        norm = np.linalg.norm(v)
        if norm == 0:
            raise ValueError("Cannot normalize zero-length vector.")
        return v / norm

    def __repr__(self):
        return f"Plane(normal={self.normal.tolist()}, offset={self.offset:.4f})"
    
    def as_plane_equation_str(self):
        """
        Return the plane equation in the form Ax + By + Cz + D = 0.

        Returns:
        - str: Plane equation
        """
        A, B, C = self.normal
        D = self.offset
        return f"{A:.4f}x + {B:.4f}y + {C:.4f}z + {D:.4f} = 0"
