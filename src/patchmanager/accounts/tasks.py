"""Background jobs for accounts."""

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string
from django.utils import translation

from patchmanager.accounts.models import User


@shared_task(autoretry_for=(OSError,), retry_backoff=True, max_retries=5)  # type: ignore[untyped-decorator]
def send_invitation(user_id: str, link: str) -> None:
    """Email an invitation link in the invited user's language."""
    user = User.objects.get(pk=user_id)
    with translation.override(user.language):
        context = {
            "user": user,
            "link": link,
            "instance_name": settings.INSTANCE_NAME,
            "hours": settings.INVITATION_TTL_HOURS,
        }
        send_mail(
            subject=render_to_string("accounts/email/invitation_subject.txt", context).strip(),
            message=render_to_string("accounts/email/invitation.txt", context),
            from_email=None,
            recipient_list=[user.email],
        )


@shared_task  # type: ignore[untyped-decorator]
def clear_expired_sessions() -> int:
    """Delete expired sessions (scheduled daily)."""
    from django.utils import timezone

    from patchmanager.accounts.models import UserSession

    deleted, _rows = UserSession.objects.filter(expire_date__lt=timezone.now()).delete()
    return deleted
