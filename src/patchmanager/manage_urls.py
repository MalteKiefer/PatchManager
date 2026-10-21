"""Admin area (``/manage/``). Not the Django admin."""

from django.urls import path

from patchmanager.accounts.views import manage
from patchmanager.audit import views as audit
from patchmanager.core.system import system_view
from patchmanager.tenancy import views as tenancy

app_name = "manage"

urlpatterns = [
    path("", manage.dashboard, name="dashboard"),
    path("users/", manage.user_list, name="users"),
    path("users/invite/", manage.user_invite, name="user-invite"),
    path("users/<uuid:pk>/", manage.user_detail, name="user"),
    path("users/<uuid:pk>/<slug:action>/", manage.user_action, name="user-action"),
    path("assignments/", manage.assignment_create, name="assignment-create"),
    path("assignments/<uuid:pk>/delete/", manage.assignment_delete, name="assignment-delete"),
    path("groups/", manage.group_list, name="groups"),
    path("groups/new/", manage.group_create, name="group-create"),
    path("groups/<uuid:pk>/", manage.group_detail, name="group"),
    path("roles/", manage.role_list, name="roles"),
    path("roles/new/", manage.role_create, name="role-create"),
    path("roles/export/", manage.role_export, name="role-export"),
    path("roles/import/", manage.role_import, name="role-import"),
    path("roles/<uuid:pk>/", manage.role_detail, name="role"),
    path("roles/<uuid:pk>/clone/", manage.role_clone, name="role-clone"),
    path("roles/<uuid:pk>/delete/", manage.role_delete, name="role-delete"),
    path("permissions/", manage.permission_matrix, name="permissions"),
    path("permissions.json", manage.permission_registry, name="permission-registry"),
    path("explain/", manage.explain_view, name="explain"),
    path("tenants/", tenancy.tenant_list, name="tenants"),
    path("tenants/new/", tenancy.tenant_create, name="tenant-create"),
    path("tenants/<uuid:pk>/", tenancy.tenant_detail, name="tenant"),
    path("audit/", audit.event_list, name="audit"),
    path("audit/export.csv", audit.event_export, name="audit-export"),
    path("audit/verify/", audit.verify_chain, name="audit-verify"),
    path("system/", system_view, name="system"),
]
