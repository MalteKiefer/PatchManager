from tests.settings import *  # noqa: F403

# The live test server speaks plain HTTP on localhost.
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_NAME = "pm_session"
CSRF_COOKIE_NAME = "pm_csrf"
