"""Gunicorn settings. Values come from the environment so the image stays generic."""

import os

bind = os.environ.get("PATCHMANAGER_BIND", "0.0.0.0:8000")  # nosec B104: container port, proxy in front
workers = int(os.environ.get("PATCHMANAGER_WORKERS", "3"))
threads = int(os.environ.get("PATCHMANAGER_THREADS", "4"))
worker_tmp_dir = "/dev/shm"  # noqa: S108  # nosec B108: tmpfs, root filesystem is read-only
timeout = 60
graceful_timeout = 30
forwarded_allow_ips = os.environ.get("PATCHMANAGER_FORWARDED_ALLOW_IPS", "127.0.0.1")
accesslog = "-"
