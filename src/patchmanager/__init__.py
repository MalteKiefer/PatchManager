"""PatchManager: self-hosted network and rack documentation."""

__version__ = "0.2.0"

from patchmanager.worker import app as celery_app

__all__ = ["__version__", "celery_app"]
