"""Tracker adapted to river debris: association by distance, not by IoU.

Why
---
Measured on the ground truth of `holtzheim_03_session_06`: between two
consecutive frames a debris item travels 33 px while its box is 24 px wide. The
consecutive boxes of a single object therefore do not overlap -- median IoU
0.000, exactly zero in 75 % of the transitions.

Every tracker shipped with Ultralytics (bytetrack, botsort, ocsort, tracktrack)
associates by IoU and **rejects** a pair whose cost exceeds `match_thresh`
before even attempting the assignment. At zero IoU the cost is 1.0: the pair is
discarded, the track dies, and the next detection opens a track that will never
activate for lack of a second match. Measured result: 1 to 2 tracks produced for
18 real objects.

This is not a lack of information. On the same ground truth, associating by the
distance between centres correctly links 99.6 % of the objects by day and 100 %
at night, against 73-76 % for a size criterion -- the area of a box varies by a
factor of 4.7 along a single track, so size is not a signature.

How
---
Only `get_dists` changes; everything else in ByteTrack (Kalman filter, two-pass
association, track life cycle) is kept.

The cost is the distance between the position **predicted by Kalman** and the
centre of the detection, normalised by the size of the detection:

    cost = min(1, distance / (max_move_ratio * sqrt(width * height)))

Normalising by the object size makes the criterion independent of resolution and
of site: it expresses "an object may travel at most N times its own size between
two frames". `match_thresh` keeps playing its role as a gate, on a [0, 1] scale
as before: 0.8 with max_move_ratio 4.0 allows 3.2 object sizes.

Registration
------------
Importing this module is enough: it adds the `river` type to Ultralytics' tracker
table. No behaviour is hijacked, `TRACKER_MAP` is a plain registration table.
"""

import numpy as np
from ultralytics.trackers import track as _ul_track
from ultralytics.trackers.byte_tracker import BYTETracker

DEFAULT_MAX_MOVE_RATIO = 4.0


class RiverTracker(BYTETracker):
    """ByteTrack whose association is done by size-normalised distance."""

    def get_dists(self, tracks, detections) -> np.ndarray:
        if not len(tracks) or not len(detections):
            return np.zeros((len(tracks), len(detections)), dtype=np.float32)

        ratio = float(getattr(self.args, "max_move_ratio", DEFAULT_MAX_MOVE_RATIO))

        t = np.array([tr.xywh for tr in tracks], dtype=np.float32)       # predicted state
        d = np.array([dt.xywh for dt in detections], dtype=np.float32)

        dist = np.linalg.norm(t[:, None, :2] - d[None, :, :2], axis=2)

        # Reference size: geometric mean of the detection sides. The detection
        # is used rather than the track, because the predicted state of a young
        # track is still noisy.
        size = np.sqrt(np.clip(d[:, 2] * d[:, 3], 1e-6, None))
        return np.clip(dist / (ratio * size[None, :]), 0.0, 1.0).astype(np.float32)


# Registration of the `river` type with Ultralytics.
_ul_track.TRACKER_MAP.setdefault("river", RiverTracker)
