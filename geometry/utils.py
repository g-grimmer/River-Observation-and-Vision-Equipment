"""Utility functions for object size estimation and camera view simulation."""
import numpy as np

# Get box vertices in 2D screen coordinates
def get_box_vertices_2D(rect_x, rect_y, rect_width, rect_height):

    rect_top_left_2D = (rect_x, rect_y)
    rect_top_right_2D = (rect_x + rect_width, rect_y)
    rect_bottom_left_2D = (rect_x, rect_y + rect_height)
    rect_bottom_right_2D = (rect_x + rect_width, rect_y + rect_height)
    rect_center_2D = (rect_x + rect_width / 2, rect_y + rect_height / 2)

    vertices = {
        'top_left': rect_top_left_2D,
        'top_right': rect_top_right_2D,
        'bottom_left': rect_bottom_left_2D,
        'bottom_right': rect_bottom_right_2D,
        'center': rect_center_2D
    }

    return vertices
