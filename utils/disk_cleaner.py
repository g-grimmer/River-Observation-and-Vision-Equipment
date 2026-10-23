import os
import glob
import shutil
import logging

logger = logging.getLogger("DiskCleaner")

def get_disk_usage(path="/"):
    """Return the usage percentage of the filesystem holding `path`."""
    try:
        total, used, free = shutil.disk_usage(path)
        return (used / total) * 100.0
    except Exception:
        return 0.0

def cleanup_old_files(directories, max_usage_percent=80.0, target_usage_percent=75.0):
    """
    Check free space. Above max_usage_percent, delete the oldest files in the
    given directories until target_usage_percent is reached.
    """
    usage = get_disk_usage()
    if usage < max_usage_percent:
        return  # nothing to do

    logger.warning(f"[DiskCleaner] Disk space alert: {usage:.1f}% (threshold: {max_usage_percent}%). Starting cleanup...")

    # Collect every .mp4, .jpg and .csv file in the directories -- the glob is
    # RECURSIVE (unlike before) so that <directory>/failed/ is included, where
    # sftp_manager._move_to_failed() parks files that exhausted their upload
    # attempts (MAX_UPLOAD_ATTEMPTS). That subdirectory used to be invisible to
    # this cleaner and could grow without bound, on the same partition as the OS,
    # never reclaimed by this very anti-saturation safeguard.
    all_files = []
    for d in directories:
        for ext in ("*.mp4", "*.jpg", "*.csv"):
            all_files.extend(glob.glob(os.path.join(d, "**", ext), recursive=True))

    # Drop the files that cannot be read
    valid_files = []
    for f in all_files:
        try:
            valid_files.append((f, os.path.getmtime(f)))
        except OSError:
            pass

    # Sort from oldest to newest
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
            logger.info(f"[DiskCleaner] Deleted (oldest): {os.path.basename(filepath)}")
        except Exception as e:
            logger.error(f"[DiskCleaner] Could not delete {filepath}: {e}")

    logger.warning(f"[DiskCleaner] Cleanup finished. {deleted_count} files deleted. "
                   f"Space reclaimed: {reclaimed_bytes / (1024*1024):.1f} MB. "
                   f"New usage: {get_disk_usage():.1f}%")
