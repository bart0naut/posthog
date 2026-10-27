import json

from posthog.test.base import APIBaseTest
from unittest.mock import MagicMock, patch

from parameterized import parameterized
from rest_framework import status

CLIENT_PATH = "products.product_analytics.backend.presentation.insight_story.build_openai_client"

FACTS = {
    "series_label": "Sign-ups",
    "insight_name": "Sign-ups",
    "chapters": [
        {"id": "peak-14", "title": "A peak on October 2", "summary": "498 on October 2.", "evidence": ["x"]},
        {"id": "latest", "title": "The latest full day", "summary": "61 on October 17.", "evidence": []},
    ],
}


def _client_returning(content: str) -> MagicMock:
    client = MagicMock()
    client.with_options.return_value.chat.completions.create.return_value.choices = [
        MagicMock(message=MagicMock(content=content))
    ]
    return client


class TestInsightStory(APIBaseTest):
    def setUp(self):
        super().setUp()
        self.organization.is_ai_data_processing_approved = True
        self.organization.save()
        self.url = f"/api/projects/{self.team.id}/insights/story/"

    @parameterized.expand(
        [
            ("matching_chapters", ["peak-14", "latest"], status.HTTP_200_OK),
            ("reordered_chapters", ["latest", "peak-14"], status.HTTP_500_INTERNAL_SERVER_ERROR),
            ("dropped_chapter", ["peak-14"], status.HTTP_500_INTERNAL_SERVER_ERROR),
        ]
    )
    def test_reading_must_match_the_chapters_sent(self, _name, ids, expected_status):
        content = json.dumps(
            {"headline": "One article caused the spike.", "chapters": [{"id": i, "reading": ""} for i in ids]}
        )
        with patch(CLIENT_PATH, return_value=_client_returning(content)):
            response = self.client.post(self.url, FACTS, format="json")

        assert response.status_code == expected_status
        if expected_status == status.HTTP_200_OK:
            assert response.json()["headline"] == "One article caused the spike."
            assert [chapter["id"] for chapter in response.json()["chapters"]] == ids

    def test_ai_not_approved_returns_403(self):
        self.organization.is_ai_data_processing_approved = False
        self.organization.save()

        with patch(CLIENT_PATH) as build_client:
            response = self.client.post(self.url, FACTS, format="json")

        assert response.status_code == status.HTTP_403_FORBIDDEN
        build_client.assert_not_called()
