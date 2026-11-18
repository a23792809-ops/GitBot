"""Task state machine with validated transitions.

States: queued → running → awaiting_review → approved → committed/failed/cancelled/expired
Read-only analysis and explanation end at the terminal ``analyzed`` /
``explained`` states and never reach ``awaiting_review``: there is nothing to
approve or commit.
Also allows failed/cancelled/expired from running/awaiting_review as appropriate.
"""

from __future__ import annotations

from dataclasses import dataclass

from gitbot.errors import InvalidStateTransition
from gitbot.models import TaskState


@dataclass(frozen=True, slots=True)
class Transition:
    from_state: TaskState
    to_state: TaskState
    allowed: bool
    reason: str | None = None


ALLOWED: dict[TaskState, set[TaskState]] = {
    TaskState.QUEUED: {TaskState.RUNNING, TaskState.CANCELLED, TaskState.FAILED},
    TaskState.RUNNING: {
        TaskState.AWAITING_REVIEW,
        TaskState.ANALYZED,
        TaskState.EXPLAINED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.EXPIRED,
    },
    TaskState.AWAITING_REVIEW: {
        TaskState.APPROVED,
        TaskState.CANCELLED,
        TaskState.EXPIRED,
        TaskState.FAILED,
    },
    TaskState.APPROVED: {TaskState.COMMITTED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.COMMITTED: set(),  # terminal
    TaskState.ANALYZED: set(),  # terminal; no commit is possible
    TaskState.EXPLAINED: set(),  # terminal; no commit is possible
    TaskState.FAILED: set(),  # terminal
    TaskState.CANCELLED: set(),  # terminal
    TaskState.EXPIRED: set(),  # terminal
}


class TaskStateMachine:
    @staticmethod
    def can_transition(from_state: TaskState, to_state: TaskState) -> bool:
        return to_state in ALLOWED.get(from_state, set())

    @staticmethod
    def transition(from_state: TaskState, to_state: TaskState) -> Transition:
        allowed = TaskStateMachine.can_transition(from_state, to_state)
        if not allowed:
            raise InvalidStateTransition(
                f"invalid state transition: {from_state} -> {to_state}",
                details={"from": from_state, "to": to_state},
            )
        return Transition(from_state=from_state, to_state=to_state, allowed=True)
