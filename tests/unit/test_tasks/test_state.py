"""Tests for task state machine."""

from __future__ import annotations

import pytest

from gitbot.errors import InvalidStateTransition
from gitbot.models import TaskState
from gitbot.tasks.state import TaskStateMachine


def test_valid_transitions() -> None:
    sm = TaskStateMachine()
    sm.transition(TaskState.QUEUED, TaskState.RUNNING)
    sm.transition(TaskState.RUNNING, TaskState.AWAITING_REVIEW)
    sm.transition(TaskState.AWAITING_REVIEW, TaskState.APPROVED)
    sm.transition(TaskState.APPROVED, TaskState.COMMITTED)


def test_invalid_transition_rejected() -> None:
    sm = TaskStateMachine()
    with pytest.raises(InvalidStateTransition):
        sm.transition(TaskState.QUEUED, TaskState.COMMITTED)
    with pytest.raises(InvalidStateTransition):
        sm.transition(TaskState.COMMITTED, TaskState.RUNNING)
    with pytest.raises(InvalidStateTransition):
        sm.transition(TaskState.FAILED, TaskState.RUNNING)


def test_cancel_from_running() -> None:
    sm = TaskStateMachine()
    sm.transition(TaskState.RUNNING, TaskState.CANCELLED)
    sm.transition(TaskState.QUEUED, TaskState.CANCELLED)
