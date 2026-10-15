"""Tracker adapte aux debris en riviere : association par distance, pas par IoU.

Pourquoi
--------
Mesure sur la verite terrain de `holtzheim_03_session_06` : entre deux trames
consecutives, un debris parcourt 33 px alors que sa boite en fait 24. Les
boites consecutives d'un meme objet ne se recouvrent donc pas -- IoU median
0,000, nul dans 75 % des transitions.

Tous les trackers livres par Ultralytics (bytetrack, botsort, ocsort,
tracktrack) apparient par IoU et **rejettent** un couple dont le cout depasse
`match_thresh` avant meme de tenter l'affectation. A IoU nul le cout vaut 1,0 :
le couple est ecarte, la piste meurt, et la detection suivante ouvre une piste
qui ne s'activera jamais faute de second appariement. Resultat mesure : 1 a 2
pistes produites pour 18 objets reels.

Ce n'est pas un manque d'information. Sur la meme verite terrain, l'affectation
par distance des centres relie correctement 99,6 % des objets le jour et 100 %
la nuit, contre 73-76 % pour un critere de taille -- l'aire d'une boite varie
d'un facteur 4,7 le long d'une seule piste, la taille n'est pas une signature.

Comment
-------
Seule `get_dists` change ; tout le reste de ByteTrack (filtre de Kalman,
association en deux passes, cycle de vie des pistes) est conserve.

Le cout est la distance entre la position **predite par Kalman** et le centre
de la detection, normalisee par la taille de la detection :

    cout = min(1, distance / (max_move_ratio * sqrt(largeur * hauteur)))

Normaliser par la taille de l'objet rend le critere independant de la
resolution et du site : il exprime « un objet peut se deplacer d'au plus N fois
sa propre taille entre deux trames ». `match_thresh` continue de jouer son role
de porte, sur une echelle [0, 1] comme avant : 0,8 avec max_move_ratio 4,0
autorise 3,2 tailles d'objet.

Enregistrement
--------------
Importer ce module suffit : il ajoute le type `river` a la table des trackers
d'Ultralytics. Aucun detournement de comportement, `TRACKER_MAP` est une simple
table d'enregistrement.
"""

import numpy as np
from ultralytics.trackers import track as _ul_track
from ultralytics.trackers.byte_tracker import BYTETracker

DEFAULT_MAX_MOVE_RATIO = 4.0


class RiverTracker(BYTETracker):
    """ByteTrack dont l'association se fait par distance normalisee a la taille."""

    def get_dists(self, tracks, detections) -> np.ndarray:
        if not len(tracks) or not len(detections):
            return np.zeros((len(tracks), len(detections)), dtype=np.float32)

        ratio = float(getattr(self.args, "max_move_ratio", DEFAULT_MAX_MOVE_RATIO))

        t = np.array([tr.xywh for tr in tracks], dtype=np.float32)       # etat predit
        d = np.array([dt.xywh for dt in detections], dtype=np.float32)

        dist = np.linalg.norm(t[:, None, :2] - d[None, :, :2], axis=2)

        # Taille de reference : moyenne geometrique des cotes de la detection.
        # On prend celle de la detection et non de la piste, car l'etat predit
        # d'une piste jeune est encore bruite.
        size = np.sqrt(np.clip(d[:, 2] * d[:, 3], 1e-6, None))
        return np.clip(dist / (ratio * size[None, :]), 0.0, 1.0).astype(np.float32)


# Enregistrement du type `river` aupres d'Ultralytics.
_ul_track.TRACKER_MAP.setdefault("river", RiverTracker)
