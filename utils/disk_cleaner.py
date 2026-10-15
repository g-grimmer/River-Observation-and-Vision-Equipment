import os
import glob
import shutil
import logging

logger = logging.getLogger("DiskCleaner")

def get_disk_usage(path="/"):
    """Renvoie le pourcentage d'utilisation du disque contenant `path`."""
    try:
        total, used, free = shutil.disk_usage(path)
        return (used / total) * 100.0
    except Exception:
        return 0.0

def cleanup_old_files(directories, max_usage_percent=80.0, target_usage_percent=75.0):
    """
    Vérifie l'espace disque. Si > max_usage_percent, supprime les fichiers les plus anciens
    dans les répertoires spécifiés jusqu'à atteindre target_usage_percent.
    """
    usage = get_disk_usage()
    if usage < max_usage_percent:
        return  # Rien à faire

    logger.warning(f"[DiskCleaner] Alerte espace disque : {usage:.1f}% (Seuil: {max_usage_percent}%). Début du nettoyage...")

    # Récupérer tous les fichiers .mp4, .jpg, .csv dans les dossiers -- glob
    # RÉCURSIF (contrairement à avant) pour inclure <dossier>/failed/, où
    # sftp_manager._move_to_failed() déplace les fichiers ayant épuisé leurs
    # tentatives d'upload (MAX_UPLOAD_ATTEMPTS). Ce sous-dossier était jusqu'ici
    # invisible à ce nettoyeur et pouvait grossir indéfiniment, sur la même
    # partition que l'OS, sans jamais être réclamé par ce garde-fou anti-saturation.
    all_files = []
    for d in directories:
        for ext in ("*.mp4", "*.jpg", "*.csv"):
            all_files.extend(glob.glob(os.path.join(d, "**", ext), recursive=True))

    # Filtrer les fichiers qui ne peuvent pas être lus
    valid_files = []
    for f in all_files:
        try:
            valid_files.append((f, os.path.getmtime(f)))
        except OSError:
            pass

    # Trier du plus vieux au plus récent
    valid_files.sort(key=lambda x: x[1])

    deleted_count = 0
    reclaimed_bytes = 0

    for filepath, mtime in valid_files:
        if get_disk_usage() <= target_usage_percent:
            break

        try:
            size = os.path.getsize(filepath)
            os.remove(filepath)
            reclaimed_bytes += size
            deleted_count += 1
            logger.info(f"[DiskCleaner] Supprimé (vieux): {os.path.basename(filepath)}")
        except Exception as e:
            logger.error(f"[DiskCleaner] Échec suppression {filepath}: {e}")

    logger.warning(f"[DiskCleaner] Nettoyage terminé. {deleted_count} fichiers supprimés. "
                   f"Espace récupéré: {reclaimed_bytes / (1024*1024):.1f} MB. "
                   f"Nouvelle utilisation: {get_disk_usage():.1f}%")
