from datetime import timedelta

from posthog.test.base import APIBaseTest
from unittest.mock import patch

from django.utils import timezone

from parameterized import parameterized

from posthog.models import Organization, OrganizationMembership, SharingConfiguration, Team, User
from posthog.models.activity_logging.activity_log import ActivityLog

from products.dashboards.backend.models.dashboard import Dashboard
from products.subscriptions.backend.models.subscription import Subscription


class TestOrganizationOffboarding(APIBaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.organization_membership.level = OrganizationMembership.Level.ADMIN
        self.organization_membership.save()
        self.departing = User.objects.create_and_join(self.organization, "departing@example.com", None)
        self.url = f"/api/organizations/@current/members/{self.departing.uuid}/"

    def test_review_finds_public_dashboards_across_projects_and_sharing_history(self) -> None:
        second_team = Team.objects.create(organization=self.organization, name="Second project")
        owned = Dashboard.objects.create(team=second_team, created_by=self.departing, name="Product overview")
        shared = Dashboard.objects.create(team=self.team, created_by=self.user, name="Weekly acquisition")
        for dashboard in (owned, shared):
            SharingConfiguration.objects.create(team=dashboard.team, dashboard=dashboard, enabled=True)
        ActivityLog.objects.create(
            team_id=self.team.pk,
            user=self.departing,
            scope="Dashboard",
            activity="sharing enabled",
            item_id=str(shared.pk),
        )
        inactive = Dashboard.objects.create(team=self.team, created_by=self.departing)
        SharingConfiguration.objects.create(team=self.team, dashboard=inactive, enabled=False)
        expired = Dashboard.objects.create(team=self.team, created_by=self.departing)
        SharingConfiguration.objects.create(
            team=self.team, dashboard=expired, enabled=True, expires_at=timezone.now() - timedelta(seconds=1)
        )
        response = self.client.get(self.url + "offboarding/")
        assert response.status_code == 200
        assert {(item["name"], item["team_id"]) for item in response.json()} == {
            ("Product overview", second_team.pk),
            ("Weekly acquisition", self.team.pk),
        }
        assert all("token" not in str(item) for item in response.json())

    @parameterized.expand([("delete",), ("post",)])
    def test_removal_disables_all_dashboard_tokens_and_preserves_other_members_links(self, method: str) -> None:
        dashboard = Dashboard.objects.create(team=self.team, created_by=self.departing, is_shared=True)
        shares = [SharingConfiguration.objects.create(team=self.team, dashboard=dashboard, enabled=True)]
        shares.append(
            SharingConfiguration.objects.create(
                team=self.team, dashboard=dashboard, enabled=True, expires_at=timezone.now() + timedelta(minutes=5)
            )
        )
        other = Dashboard.objects.create(team=self.team, created_by=self.user)
        other_share = SharingConfiguration.objects.create(team=self.team, dashboard=other, enabled=True)
        response = (
            self.client.delete(self.url)
            if method == "delete"
            else self.client.post(self.url + "offboard/", {}, format="json")
        )
        assert response.status_code == 204
        assert not OrganizationMembership.objects.filter(user=self.departing, organization=self.organization).exists()
        for share in shares:
            share.refresh_from_db()
            assert not share.enabled
        dashboard.refresh_from_db()
        other_share.refresh_from_db()
        assert not dashboard.is_shared
        assert other_share.enabled

    def test_admin_can_disable_selected_dashboards(self) -> None:
        kept = Dashboard.objects.create(team=self.team, created_by=self.departing)
        disabled = Dashboard.objects.create(team=self.team, created_by=self.departing)
        kept_share = SharingConfiguration.objects.create(team=self.team, dashboard=kept, enabled=True)
        disabled_share = SharingConfiguration.objects.create(team=self.team, dashboard=disabled, enabled=True)
        response = self.client.post(
            self.url + "offboard/", {"keep": [f"public_link:dashboard:{self.team.pk}:{kept.pk}"]}, format="json"
        )
        assert response.status_code == 204
        kept_share.refresh_from_db()
        disabled_share.refresh_from_db()
        assert kept_share.enabled
        assert not disabled_share.enabled

    def test_review_and_cleanup_are_scoped_to_organization(self) -> None:
        other_team = Team.objects.create(organization=Organization.objects.create(name="Other organization"))
        dashboard = Dashboard.objects.create(team=other_team, created_by=self.departing)
        share = SharingConfiguration.objects.create(team=other_team, dashboard=dashboard, enabled=True)
        other_subscription = Subscription.objects.create(
            team=other_team,
            created_by=self.departing,
            target_type="email",
            target_value=self.departing.email,
            frequency="daily",
            interval=1,
            start_date=timezone.now(),
        )
        assert self.client.get(self.url + "offboarding/").json() == []
        response = self.client.post(
            self.url + "offboard/", {"keep": [f"public_link:dashboard:{other_team.pk}:{dashboard.pk}"]}, format="json"
        )
        assert response.status_code == 400
        assert OrganizationMembership.objects.filter(user=self.departing, organization=self.organization).exists()
        assert self.client.delete(self.url).status_code == 204
        share.refresh_from_db()
        assert share.enabled
        other_subscription.refresh_from_db()
        assert other_subscription.enabled

    def test_member_cannot_review_or_remove_another_member(self) -> None:
        self.organization_membership.level = OrganizationMembership.Level.MEMBER
        self.organization_membership.save()
        assert self.client.get(self.url + "offboarding/").status_code == 403
        assert self.client.post(self.url + "offboard/", {}, format="json").status_code == 403

    def test_self_removal_cannot_keep_public_links(self) -> None:
        dashboard = Dashboard.objects.create(team=self.team, created_by=self.user)
        share = SharingConfiguration.objects.create(team=self.team, dashboard=dashboard, enabled=True)
        url = f"/api/organizations/@current/members/{self.user.uuid}/offboard/"
        assert (
            self.client.post(
                url, {"keep": [f"public_link:dashboard:{self.team.pk}:{dashboard.pk}"]}, format="json"
            ).status_code
            == 403
        )
        assert self.client.post(url, {}, format="json").status_code == 204
        share.refresh_from_db()
        assert not share.enabled

    @patch("posthog.api.sharing.export_asset_for_opengraph")
    def test_publishers_survive_rotation_without_activity_history(self, _export: object) -> None:
        dashboard = Dashboard.objects.create(team=self.team, created_by=self.user)
        self.client.force_login(self.departing)
        response = self.client.patch(
            f"/api/projects/{self.team.pk}/dashboards/{dashboard.pk}/sharing/", {"enabled": True}, format="json"
        )
        assert response.status_code == 200
        share = SharingConfiguration.objects.get(team=self.team, dashboard=dashboard)
        assert share.shared_by.filter(pk=self.departing.pk).exists()
        rotated = share.rotate_access_token()
        assert rotated.shared_by.filter(pk=self.departing.pk).exists()
        ActivityLog.objects.filter(team_id=self.team.pk, user=self.departing, scope="Dashboard").delete()
        self.client.force_login(self.user)
        response = self.client.get(self.url + "offboarding/")
        assert response.status_code == 200
        assert [item["id"] for item in response.json()] == [f"public_link:dashboard:{self.team.pk}:{dashboard.pk}"]

    def subscription(self, *, creator: User, recipients: str) -> Subscription:
        return Subscription.objects.create(
            team=self.team,
            created_by=creator,
            title="Weekly report",
            target_type="email",
            target_value=recipients,
            frequency="daily",
            interval=1,
            start_date=timezone.now(),
        )

    @parameterized.expand([("delete",), ("post",)])
    def test_removal_disables_created_subscriptions_and_removes_email_recipients(self, method: str) -> None:
        own = self.subscription(creator=self.departing, recipients="other@example.com")
        others = self.subscription(creator=self.user, recipients="other@example.com")
        recipient = self.subscription(creator=self.user, recipients="DEPARTING@example.com,other@example.com")
        only_recipient = self.subscription(creator=self.user, recipients=self.departing.email)
        response = self.client.get(self.url + "offboarding/")
        assert response.status_code == 200
        assert [item["id"] for item in response.json()] == [f"subscription:{own.pk}"]
        response = (
            self.client.delete(self.url)
            if method == "delete"
            else self.client.post(self.url + "offboard/", {}, format="json")
        )
        assert response.status_code == 204
        for subscription in (own, others, recipient, only_recipient):
            subscription.refresh_from_db()
        assert not own.enabled
        assert others.enabled
        assert recipient.enabled and recipient.target_value == "other@example.com"
        assert not only_recipient.enabled and only_recipient.target_value == ""

    def test_kept_subscriptions_still_remove_the_departing_recipient(self) -> None:
        kept = self.subscription(creator=self.departing, recipients=f"{self.departing.email},other@example.com")
        disabled = self.subscription(creator=self.departing, recipients="other@example.com")
        response = self.client.post(self.url + "offboard/", {"keep": [f"subscription:{kept.pk}"]}, format="json")
        assert response.status_code == 204
        kept.refresh_from_db()
        disabled.refresh_from_db()
        assert kept.enabled and kept.target_value == "other@example.com"
        assert not disabled.enabled
