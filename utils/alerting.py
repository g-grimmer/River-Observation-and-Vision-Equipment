"""
alerting.py
-----------
Canal d'alerte minimal pour un système sans supervision humaine directe.

Aucune notification externe (SMS/email/webhook) n'est configurée pour l'instant :
ce module écrit un fichier texte horodaté dans LOG_DIR, qui remonte au NAS via le
mécanisme d'upload SFTP existant dès que la connexion le permet. C'est un strict
minimum — l'opérateur doit consulter le NAS régulièrement pour voir ces alertes.
"""

import os
import datetime
from utils.configs import LOG_DIR


def write_alert(source: str, message: str) -> str:
    """Écrit un fichier ALERTE_<source>_<timestamp>.txt dans LOG_DIR.

    Retourne le chemin du fichier créé (chaîne vide en cas d'échec d'écriture),
    à passer ensuite à SFTPManager.upload_file() pour qu'il remonte au NAS.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(LOG_DIR, f"ALERTE_{source}_{ts}.txt")
    try:
        with open(path, "w") as f:
            f.write(f"[{datetime.datetime.now().isoformat()}] {message}\n")
    except Exception as e:
        print(f"⚠️ [Alerte] Impossible d'écrire le fichier d'alerte : {e}")
        return ""
    print(f"🚨 ALERTE : {message}")
    return path
