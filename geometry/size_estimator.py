import math

from .intersection import get_largest_diameters


def estimate_size_m(camera, x1, y1, x2, y2, portee_max_m=None):
    """Taille physique de l'objet en metres, depuis la boite YOLO.

    Renvoie (nan, nan) quand la boite n'est pas exploitable geometriquement.
    `tracker.py` teste deja ce cas (`geometry_valid`) et exclut les NaN des
    moyennes : une valeur absurde mais finie, elle, entrait silencieusement
    dans les statistiques.

    Deux garde-fous, tous deux portant sur le bord SUPERIEUR de la boite, qui
    fournit le rayon le plus rasant :

    1. Au-dessus de l'horizon, le rayon ne coupe jamais le plan d'eau. Avec la
       pose declaree (H=2.7 m, tangage -19 deg, demi-champ vertical 20.95 deg),
       le haut de l'image pointe 1.95 deg au-dessus de l'horizontale : les 5 %
       superieurs de l'image sont dans ce cas.

    2. Juste sous l'horizon, le rayon est rasant et son intersection part au
       loin : a y_c = 0.10 la portee vaut 69 m et un seul pixel de boite
       deplace l'estimation de 5.3 cm, contre 0.33 cm en bas d'image.
       `portee_max_m` doit valoir la distance de la berge opposee : au-dela,
       il n'y a plus d'eau, donc plus de debris a mesurer.

    Retourne: (largeur_m, hauteur_m)
    """
    _, direction = camera.get_world_ray((x1 + x2) / 2.0, y1)

    # get_world_ray renvoie un vecteur unitaire ; le plan d'eau est y = 0 et la
    # camera est en y = H (voir intersection.ray_plane_intersection_y0).
    if direction[1] >= 0:
        return float("nan"), float("nan")

    if portee_max_m is not None:
        portee = camera.position[1] / (-direction[1])
        if portee > portee_max_m:
            return float("nan"), float("nan")

    return get_largest_diameters(camera, x1, y1, x2 - x1, y2 - y1)
