from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import CharField, OuterRef, Q
from django.db.models.functions import Cast

from posthog.models.activity_logging.activity_log import ActivityLog
from posthog.models.organization import OrganizationMembership
from posthog.models.sharing_configuration import SharingConfiguration
from posthog.organization_offboarding_types import OffboardingResource

from products.subscriptions.backend.facade import api as subscriptions


class OrganizationOffboarding:
    def __init__(self, membership: OrganizationMembership) -> None:
        self.membership = membership
        self.organization_id = str(membership.organization_id)
        self.user_id = membership.user_id

    def resources(self) -> list[OffboardingResource]:
        shared_ids = ActivityLog.objects.filter(
            team_id=OuterRef("team_id"),
            user_id=self.user_id,
            scope="Dashboard",
            activity="sharing enabled",
        ).values("item_id")
        configurations = (
            SharingConfiguration.objects.filter(
                SharingConfiguration.tokens_active_q(),
                team__organization_id=self.organization_id,
                dashboard__deleted=False,
            )
            .annotate(dashboard_log_id=Cast("dashboard_id", output_field=CharField()))
            .filter(
                Q(dashboard__created_by_id=self.user_id)
                | Q(shared_by__id=self.user_id)
                | Q(dashboard_log_id__in=shared_ids),
            )
            .select_related("dashboard", "team")
            .distinct()
        )
        resources = {
            f"public_link:dashboard:{configuration.team_id}:{configuration.dashboard_id}": OffboardingResource(
                id=f"public_link:dashboard:{configuration.team_id}:{configuration.dashboard_id}",
                kind="dashboard",
                name=configuration.dashboard.name or "Untitled dashboard",
                team_id=configuration.team_id,
                project_name=configuration.team.name,
            )
            for configuration in configurations
            if configuration.dashboard
        }
        return sorted(
            [*resources.values(), *subscriptions.list_member_offboarding_resources(self.organization_id, self.user_id)],
            key=lambda resource: (resource.kind, resource.name, resource.id),
        )

    def remove(self, keep: set[str]) -> None:
        if self.membership.level == OrganizationMembership.Level.OWNER:
            raise ValidationError("Cannot leave the organization as its owner!")
        with transaction.atomic():
            for resource in self.resources():
                if resource.kind == "dashboard" and resource.id not in keep:
                    dashboard_id = resource.id.rsplit(":", 1)[1]
                    configurations = SharingConfiguration.objects.filter(
                        team_id=resource.team_id, dashboard_id=dashboard_id
                    )
                    configurations.update(enabled=False)
                    configuration = configurations.select_related("dashboard").first()
                    if configuration and configuration.dashboard:
                        configuration.dashboard.is_shared = False
                        configuration.dashboard.save(update_fields=["is_shared"])
            subscriptions.disable_member_offboarding_resources(
                self.organization_id,
                self.user_id,
                keep,
                self.membership.user.email,
            )
            self.membership.user.leave(organization=self.membership.organization)
