import datetime
import os
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- Camera Configuration ---
# HI_RES_SIZE is for Video Recording (Compromise for 12MP Module V3)
# 1280x720 (720p) is lighter for storage and 4G uploads.
HI_RES_SIZE = (1280, 720)

# FRAME_SIZE is for YOLO Inference (Standard)
# Doit conserver le ratio du capteur (2304x1296 = 16:9). Un format carre
# appliquait deux facteurs d'echelle differents -- 0,417 en x, 0,741 en y --
# et etirait tout de 1,8x en hauteur : un debris carre de 30x28 px devenait un
# rectangle de 12x21. Les modeles ayant ete entraines sur des images mises aux
# normes par letterbox (ratio preserve), ils voyaient en production des formes
# qu'ils n'avaient jamais apprises. Mesure : AP@0.5 0,900 -> 0,926 le jour et
# 0,782 -> 0,821 la nuit. Ultralytics fait lui-meme le letterbox vers 960x960,
# donc l'inference coute exactement pareil.
FRAME_SIZE = (960, 540)

# Recording Settings
VIDEO_FPS = 3.0

# Cadence d'acquisition du thread camera, alignee sur VIDEO_FPS.
# Capturer plus vite ne sert a rien : la boucle d'analyse ne consomme pas
# davantage et le surplus etait jete par la file bornee -- 33 % des images
# produites, mesure au pipeline. Le CPU ainsi libere profite a l'inference.
CAPTURE_FPS = VIDEO_FPS
VIDEO_CODEC = "mp4v" # or "XVID"
VIDEO_EXTENSION = "mp4"

# Two cameras CSI Raspberry Pi
# 0: RGB  (regime diurne, voir HEURE_JOUR_DEBUT/FIN)
# 1: NoIR (regime nocturne)
CAMERA_IDS = [0, 1]

# --- Plage horaire du regime diurne -----------------------------------------
# Le regime diurne va de HEURE_JOUR_DEBUT (incluse) a HEURE_JOUR_FIN (exclue) ;
# tout le reste de la journee est nocturne. Exprime ainsi, et non par un seul
# seuil, parce qu'un unique `heure < 20` laissait le creneau de minuit a 20h en
# regime diurne : de minuit a 8h en hiver, l'instrument observait dans le noir
# avec la camera RGB, le modele du jour et le reglage de suivi du jour.
HEURE_JOUR_DEBUT = 7   # 07:00 -> camera 0 (RGB) + modele diurne
HEURE_JOUR_FIN   = 19  # 19:00 -> camera 1 (NoIR) + modele nocturne


def est_jour(moment=None) -> bool:
    """True si `moment` (datetime, defaut maintenant) tombe dans le regime diurne.

    Source unique de la decision jour/nuit : elle etait auparavant ecrite deux
    fois dans main.py (une fois au demarrage, une fois dans select_camera), avec
    le risque qu'une modification n'en corrige qu'une et que le modele charge ne
    corresponde plus a la camera selectionnee.
    """
    heure = (moment or datetime.datetime.now()).hour
    return HEURE_JOUR_DEBUT <= heure < HEURE_JOUR_FIN

# --- AI Configuration ---
# Bascule automatique jour/nuit (voir est_jour ci-dessus) : caméra 0 (RGB, de 7h
# à 19h) -> modèle diurne, caméra 1 (NoIR, de 19h à 7h) -> modèle nocturne. MODEL_PATH
# reste le modèle par défaut utilisé quand aucune bascule n'est pertinente (ex: le
# tout premier chargement avant la sélection de caméra) ou quand --model est passé
# explicitement en ligne de commande (dans ce cas la bascule automatique est désactivée).
# Ancrés sur BASE_DIR (pas relatifs au répertoire courant du process) : un
# lancement manuel depuis un autre répertoire (intervention SSH à distance) ne
# doit pas faire planter le chargement du modèle avec un FileNotFoundError.
# YOLO26n monoclasse 960x960, exports NCNN (05_RESULTS/04_NCNN).
# NCNN et non .pt : mesure a 157 ms contre 460 ms pour le PyTorch 26s.
MODEL_PATH_DAY = os.path.join(BASE_DIR, "models_yolo26n", "yolo26n_day_best_ncnn_model")
MODEL_PATH_NIGHT = os.path.join(BASE_DIR, "models_yolo26n", "yolo26n_night_best_ncnn_model")
MODEL_PATH = MODEL_PATH_DAY
CONFIDENCE = 0.10

# Resolution d'inference, transmise a Ultralytics par YoloDetector.
IMGSZ = 960

# --- Pose de la caméra ------------------------------------------------------
# Centralisée ici parce qu'elle était dupliquée dans main.py et dans le harnais
# d'évaluation, avec des valeurs divergentes. La taille estimée est strictement
# linéaire en CAMERA_HAUTEUR_M : deux valeurs différentes donnent deux jeux de
# mesures incomparables.
#
# ATTENTION : ces valeurs viennent du commentaire "Fixed for test" de main.py.
# CAMERA_HAUTEUR_M doit être relevée au-dessus du ZÉRO DE L'ÉCHELLE
# LIMNIMÉTRIQUE, et non au-dessus de l'eau du jour d'installation, sinon la
# correction hydrologique n'a pas de référentiel.
# Sensibilité mesurée : 1° de tangage = 3,7 % de taille ; 10 cm de hauteur = 3,7 %.
CAMERA_HAUTEUR_M = 2.7
CAMERA_TANGAGE_DEG = -19.0
CAMERA_FOCALE_MM = 4.74
CAMERA_CAPTEUR_MM = (6.45, 3.63)

# Portée maximale au-delà de laquelle l'estimation de taille est rejetée.
# À régler sur la distance de la berge opposée : plus loin, il n'y a plus d'eau.
# Sous l'horizon les rayons deviennent rasants et l'estimation diverge — à la
# pose actuelle, la portée vaut 69 m à y_c = 0,10 contre 4 m en bas d'image.
# 20 m correspond ici à y_c ≈ 0,22 ; à ajuster après relevé du site.
PORTEE_MAX_M = 20.0

# Configuration de suivi, ancrée sur BASE_DIR pour la même raison.
# Deux fichiers : le déplacement inter-trame mesuré vaut 1,38 fois la taille de
# l'objet le jour contre 0,31 la nuit, et le réglage optimal en dépend
# (max_move_ratio 6,0 le jour, 4,0 la nuit).
TRACKER_CONFIG_DAY = os.path.join(BASE_DIR, "river_day.yaml")
TRACKER_CONFIG_NIGHT = os.path.join(BASE_DIR, "river_night.yaml")
TRACKER_CONFIG_PATH = TRACKER_CONFIG_DAY   # défaut, avant la première bascule

# Rotation Settings
ROTATION_MINUTES = 5  # Duration of each video sequence (minutes)

# --- Local Storage ---
VIDEO_DIR = os.path.join(BASE_DIR, "videos")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# Ensure dirs exist
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# --- SFTP Configuration ---
# Volontairement vides dans le depot. Ces quatre valeurs sont renseignees a
# l'installation par setup_agent.sh (section « NAS / SFTP Configuration »), qui
# les reecrit ici. Les laisser en clair dans le code revenait a les publier dans
# l'historique git, d'ou on ne peut plus les retirer apres coup.
# Un hote vide desactive de fait les transferts : SFTPManager le signale au
# demarrage et les fichiers restent sur la carte SD au lieu de partir.
SFTP_HOST = ""
SFTP_PORT = 22
SFTP_USERNAME = ""
SFTP_PASSWORD = ""
SFTP_REMOTE_DIR = ""
