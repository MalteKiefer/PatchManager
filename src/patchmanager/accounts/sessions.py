"""Database session store that records owner and metadata in columns."""

from typing import Any

from django.contrib.auth import SESSION_KEY
from django.contrib.sessions.backends.db import SessionStore as DBStore
from django.utils.dateparse import parse_datetime

# Session data keys maintained by services.auth and SessionPolicyMiddleware.
IP = "_pm_ip"
USER_AGENT = "_pm_ua"
CREATED = "_pm_created"
LAST_ACTIVITY = "_pm_last"


class SessionStore(DBStore):
    """Copies selected session data into indexed columns on every save."""

    @classmethod
    def get_model_class(cls) -> Any:
        """Use :class:`patchmanager.accounts.models.UserSession`."""
        from patchmanager.accounts.models import UserSession

        return UserSession

    def create_model_instance(self, data: dict[str, Any]) -> Any:
        """Build the row, including owner, address and timestamps."""
        obj: Any = super().create_model_instance(data)
        obj.user_id = data.get(SESSION_KEY)
        obj.ip_address = data.get(IP)
        obj.user_agent = data.get(USER_AGENT, "")
        obj.created_at = parse_datetime(data[CREATED]) if data.get(CREATED) else None
        obj.last_activity = parse_datetime(data[LAST_ACTIVITY]) if data.get(LAST_ACTIVITY) else None
        return obj
