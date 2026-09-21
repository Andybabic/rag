"""In-flight consent for skill functions marked ``ask for permission``.

The chat is a single streaming POST, so a question asked mid-answer needs a
side channel: the pipeline emits a ``permission_request`` event and then waits
on an :class:`asyncio.Event` that a second request — the user clicking
"Erlauben" — resolves. This registry is the rendezvous between the two.

Deliberately in-process and deliberately not durable. A pending request is only
meaningful while the stream that asked it is still open; if the process
restarts, the stream is gone too and a stored approval would be an approval
nobody is waiting for. Anything that outlives the request should be read from
``skill_executions`` instead.

Denial is the default in every ambiguous case: timeout, disconnect, or a
restart all end as "not approved", so code never runs because a user failed to
answer.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: How long a request waits for a click before giving up. Long enough to read
#: the question, short enough that an abandoned tab does not pin a worker.
DEFAULT_TIMEOUT_S = 120.0

#: Pending requests are small, but an unbounded dict is still a leak if a
#: caller ever forgets to resolve one. Sweeping on every registration keeps the
#: cost proportional to traffic.
_MAX_AGE_S = 600.0


@dataclass
class PendingPermission:
    call_id: str
    session_id: str
    use_case: str
    skill: str
    function: str
    args: tuple
    description: str
    created_at: float = field(default_factory=time.monotonic)
    approved: bool | None = None
    decided_by: str | None = None
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


_pending: dict[str, PendingPermission] = {}


def _sweep() -> None:
    now = time.monotonic()
    for call_id, pending in list(_pending.items()):
        if now - pending.created_at > _MAX_AGE_S:
            _pending.pop(call_id, None)


def register(
    *,
    session_id: str,
    use_case: str,
    skill: str,
    function: str,
    args: tuple,
    description: str = "",
) -> PendingPermission:
    """Create a pending request and return it. The caller then awaits it."""
    _sweep()
    pending = PendingPermission(
        call_id=str(uuid.uuid4()),
        session_id=session_id,
        use_case=use_case,
        skill=skill,
        function=function,
        args=args,
        description=description,
    )
    _pending[pending.call_id] = pending
    return pending


def resolve(call_id: str, approved: bool, *, session_id: str = "",
            username: str | None = None) -> bool:
    """Record a user's decision. Returns False if nothing is waiting.

    *session_id*, when given, must match the session that asked — otherwise one
    user's client could approve code execution in another user's conversation
    just by guessing a call id.
    """
    pending = _pending.get(call_id)
    if pending is None:
        return False
    if session_id and pending.session_id and session_id != pending.session_id:
        logger.warning(
            "Permission %s answered from a different session — ignored", call_id
        )
        return False
    pending.approved = approved
    pending.decided_by = username
    pending._event.set()
    return True


async def wait_for(
    pending: PendingPermission, timeout: float = DEFAULT_TIMEOUT_S
) -> bool:
    """Block until the user decides, or the wait times out.

    Returns True only on an explicit approval.
    """
    try:
        await asyncio.wait_for(pending._event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.info(
            "Permission request %s (%s.%s) timed out after %.0fs — treated as denied",
            pending.call_id, pending.skill, pending.function, timeout,
        )
        pending.approved = False
    finally:
        _pending.pop(pending.call_id, None)
    return bool(pending.approved)


def cancel(call_id: str) -> None:
    """Drop a pending request without a decision (e.g. the stream died)."""
    _pending.pop(call_id, None)


def pending_count() -> int:
    return len(_pending)
