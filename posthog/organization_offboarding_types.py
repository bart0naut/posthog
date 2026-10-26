from posthog.dataclasses import frozen


@frozen
class OffboardingResource:
    id: str
    kind: str
    name: str
    team_id: int
    project_name: str
