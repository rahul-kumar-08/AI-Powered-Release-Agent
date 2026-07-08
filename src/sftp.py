"""Stage 6: SFTP upload to hoth."""

import os

from src.config import _get_env, BASE_URL
from src.logger import Log
import paramiko


def _sftp_makedirs(sftp, remote_dir):
    """Recursively create directories on the SFTP server."""
    dirs_to_create = []
    current = remote_dir
    while current and current not in ("/", "."):
        try:
            sftp.stat(current)
            break
        except IOError:
            dirs_to_create.append(current)
            current = os.path.dirname(current)

    for d in reversed(dirs_to_create):
        try:
            sftp.mkdir(d)
        except IOError:
            pass


def upload_to_sftp(rows, output_dir, filter_type="all"):
    """Upload generated changelog.txt and rpm.txt to the SFTP server."""

    host = _get_env("SFTP_HOST")
    username = _get_env("SFTP_USERNAME")
    password = _get_env("SFTP_PASSWORD")
    port = int(_get_env("SFTP_PORT", "22"))
    remote_base = _get_env("SFTP_REMOTE_PATH") or _get_env("SFTP_REMOTE_BASE")

    if not host or not username:
        Log.error("SFTP upload skipped: SFTP_HOST or SFTP_USERNAME not set in .env")
        return []

    transport = None
    sftp = None
    uploaded = []

    try:
        transport = paramiko.Transport((host, port))
        transport.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)

        for row in rows:
            version = row.get("goldimage_version", "unknown")
            rtype = row.get("type", "AOS")

            file_pairs = [
                ("changelog.txt", "changelog_url"),
                ("rpm.txt", "rpm_url"),
            ]
            if row.get("gi_tarball_url"):
                file_pairs.append(("pcvm.tar.xz", "gi_tarball_url"))

            for filename, url_key in file_pairs:
                url = row.get(url_key, "")
                if not url or url == "Data not found":
                    continue

                local_path = os.path.join(output_dir, version, rtype, filename)
                if not os.path.isfile(local_path):
                    if filename == "pcvm.tar.xz":
                        Log.error(f"[{rtype}] {filename} not found locally at "
                                  f"{local_path} — Artifactory download may have failed")
                    continue

                relative = url.replace(BASE_URL, "").lstrip("/")
                if remote_base:
                    remote_path = f"{remote_base.rstrip('/')}/{relative}"
                else:
                    remote_path = relative

                remote_dir = os.path.dirname(remote_path)
                _sftp_makedirs(sftp, remote_dir)

                sftp.put(local_path, remote_path)
                uploaded.append({
                    "rtype": rtype, "version": version,
                    "file": filename, "remote_path": remote_path,
                })
                Log.info(f"[{rtype}] Uploaded {filename} → sftp://{host}{remote_path}")

    except Exception as e:
        Log.error(f"SFTP upload error: {e}")
    finally:
        if sftp:
            sftp.close()
        if transport:
            transport.close()

    return uploaded
