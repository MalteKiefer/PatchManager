from django.urls import path

from patchmanager.accounts.views import account, auth

app_name = "accounts"

urlpatterns = [
    path("setup/", auth.setup_view, name="setup"),
    path("auth/login/", auth.login_view, name="login"),
    path("auth/mfa/", auth.mfa_view, name="mfa"),
    path("auth/webauthn/options/", auth.webauthn_login_options, name="webauthn-login-options"),
    path("auth/webauthn/verify/", auth.webauthn_login_verify, name="webauthn-login-verify"),
    path("auth/logout/", auth.logout_view, name="logout"),
    path("invite/<str:token>/", auth.invitation_view, name="invitation"),
    path("account/", account.profile_view, name="profile"),
    path("account/security/", account.security_view, name="security"),
    path("account/my-data/", account.data_export_view, name="data-export"),
    path("account/mfa/totp/", account.totp_setup_view, name="totp-setup"),
    path(
        "account/mfa/webauthn/options/",
        account.webauthn_register_options,
        name="webauthn-register-options",
    ),
    path("account/mfa/webauthn/register/", account.webauthn_register, name="webauthn-register"),
    path("account/mfa/recovery/", account.recovery_codes_view, name="recovery-codes"),
    path(
        "account/mfa/<str:kind>/<uuid:factor_id>/remove/",
        account.remove_factor_view,
        name="remove-factor",
    ),
    path(
        "account/sessions/<str:session_key>/revoke/",
        account.revoke_session_view,
        name="revoke-session",
    ),
    path("account/sessions/revoke-others/", account.revoke_all_view, name="revoke-others"),
    path("account/impersonation/stop/", account.stop_impersonation_view, name="stop-impersonation"),
]
