"""The authorization session: the current user, and the lookups every check in one scope shares."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import field
from typing import TYPE_CHECKING, Any

from posthog.dataclasses import frozen

if TYPE_CHECKING:
    from posthog.models import Team, User

    from products.access_control.backend.facade.user_access_control import UserAccessControl

PrincipalKey = tuple[str, int]


@frozen
class Session:
    """Lookups that last for one request or one `authorization_session()` block.

    `principal` returns the request's user when a check runs. It does not read the user when the
    request starts, because DRF authenticates the request after the middleware runs.
    """

    principal: Callable[[], Any] | None = None
    users: dict[int, User | None] = field(default_factory=dict)
    teams: dict[int, Team | None] = field(default_factory=dict)
    access: dict[tuple[PrincipalKey, int], UserAccessControl] = field(default_factory=dict)

    def clear(self) -> None:
        self.users.clear()
        self.teams.clear()
        self.access.clear()


_current: ContextVar[Session | None] = ContextVar("authorization_session", default=None)


def current() -> Session | None:
    return _current.get()


@contextmanager
def scope(principal: Callable[[], Any] | None = None) -> Iterator[Session]:
    """Open a session until the block exits. A block without a principal reuses a session that is already open."""
    active = _current.get()
    if active is not None and principal is None:
        yield active
        return
    opened = Session(principal=principal)
    token = _current.set(opened)
    try:
        yield opened
    finally:
        _current.reset(token)


def invalidate(**_: Any) -> None:
    """Drop the cached rules after a rule, role or membership write, so the next check reads them again."""
    session = _current.get()
    if session is not None:
        session.clear()
