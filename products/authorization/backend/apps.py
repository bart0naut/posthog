"""Django app configuration for authorization."""

from django.apps import AppConfig
from django.db.models.signals import post_delete, post_save

from .logic import session

# Writes to these models change what the current access rules answer, so they clear the
# rules a session has already loaded.
RULE_MODELS = ("ee.AccessControl", "ee.RoleMembership", "posthog.OrganizationMembership")


class AuthorizationConfig(AppConfig):
    name = "products.authorization.backend"
    label = "authorization"

    def ready(self) -> None:
        for model in RULE_MODELS:
            post_save.connect(session.invalidate, sender=model, dispatch_uid=f"authorization-save-{model}")
            post_delete.connect(session.invalidate, sender=model, dispatch_uid=f"authorization-delete-{model}")
