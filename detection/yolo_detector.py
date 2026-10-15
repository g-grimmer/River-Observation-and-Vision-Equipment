from ultralytics import YOLO
from utils.configs import TRACKER_CONFIG_PATH

# L'import enregistre le type de tracker `river` aupres d'Ultralytics.
from detection import river_tracker  # noqa: F401

class YoloDetector:
    def __init__(self, model_path, conf, imgsz=None, tracker=None):
        self.model = YOLO(model_path)
        self.conf = conf
        # Le reglage de suivi depend du regime : voir TRACKER_CONFIG_DAY/NIGHT.
        self.tracker = tracker or TRACKER_CONFIG_PATH
        # Les exports NCNN sont figes a leur resolution d'entrainement (960).
        # Sans imgsz explicite, Ultralytics retombe sur son defaut de 640 et
        # le modele travaille hors de la resolution pour laquelle il a ete
        # entraine -- perte de rappel sur les petits objets, qui sont ici la
        # quasi-totalite des cibles.
        self.imgsz = imgsz

    def predict(self, frame):
        # Utilisation de TRACK pour le suivi (ByteTrack par défaut ou spécifié)
        # persist=True est important pour maintenir les IDs entre les frames
        # TRACKER_CONFIG_PATH est ancré sur BASE_DIR (utils/configs.py) : un
        # chemin relatif ici dépendait du répertoire courant du process, pas de
        # l'emplacement du dépôt -- cassait avec un FileNotFoundError si l'agent
        # était lancé manuellement depuis un autre répertoire.
        kw = dict(conf=self.conf, persist=True,
                  tracker=self.tracker, verbose=False)
        if self.imgsz:
            kw["imgsz"] = self.imgsz
        return self.model.track(frame, **kw)
