"""
alerting.py
-----------
Minimal alert channel for a system with no direct human supervision.

No external notification (SMS/email/webhook) is configured for now: this module
writes a timestamped text file into LOG_DIR, which travels to the NAS through the
existing SFTP upload mechanism as soon as the link allows. It is a bare minimum --
the operator has to check the NAS regularly to see these alerts.
"""

import os
import datetime
from utils.configs import LOG_DIR


def write_alert(source: str, message: str) -> str:
    """Write an ALERTE_<source>_<timestamp>.txt file into LOG_DIR.

    Returns the path of the created file (an empty string if the write failed),
    to be passed on to SFTPManager.upload_file() so it reaches the NAS.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(LOG_DIR, f"ALERTE_{source}_{ts}.txt")
    try:
        with open(path, "w") as f:
            f.write(f"[{datetime.datetime.now().isoformat()}] {message}\n")
    except Exception as e:
        print(f"⚠️ [Alert] Could not write the alert file: {e}")
        return ""
    print(f"🚨 ALERT: {message}")
    return path
