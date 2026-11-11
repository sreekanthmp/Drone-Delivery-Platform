"""AuditEvent: immutability and consistency (Rule 5)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.domain.exceptions import AuditEventValidationError
from app.domain.models import AuditEvent, AuditEventType, EventSource, OrderStatus

S = OrderStatus
T = AuditEventType
NOW = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)


def make_event(**overrides: Any) -> AuditEvent:
    fields: dict[str, Any] = {
        "event_id": UUID(int=1),
        "order_id": UUID(int=2),
        "event_type": T.ORDER_STATUS_CHANGED,
        "previous_state": S.CREATED,
        "new_state": S.ASSIGNED,
        "occurred_at": NOW,
        "source": EventSource.DISPATCH,
        "drone_id": UUID(int=3),
        "metadata": {},
    }
    fields.update(overrides)
    return AuditEvent(**fields)


class TestFields:
    def test_carries_every_field_required_by_the_audit_rule(self) -> None:
        event = make_event(metadata={"k": "v"})
        assert event.event_id == UUID(int=1)
        assert event.order_id == UUID(int=2)
        assert event.drone_id == UUID(int=3)
        assert event.event_type is T.ORDER_STATUS_CHANGED
        assert event.previous_state is S.CREATED
        assert event.new_state is S.ASSIGNED
        assert event.occurred_at == NOW
        assert event.metadata == {"k": "v"}
        assert event.source is EventSource.DISPATCH

    def test_drone_id_and_metadata_are_optional(self) -> None:
        event = AuditEvent(
            event_id=UUID(int=1),
            order_id=UUID(int=2),
            event_type=T.ORDER_STATUS_CHANGED,
            previous_state=S.CREATED,
            new_state=S.CANCELLED,
            occurred_at=NOW,
            source=EventSource.API,
        )
        assert event.drone_id is None
        assert event.metadata == {}

    def test_event_sources(self) -> None:
        assert {s.value for s in EventSource} == {
            "api",
            "adapter",
            "dispatch",
            "telemetry",
            "webhook",
            "ops",
            "system",
        }


class TestImmutability:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("new_state", S.DELIVERED),
            ("previous_state", S.FAILED),
            ("event_type", T.ORDER_CREATED),
            ("occurred_at", datetime(2000, 1, 1, tzinfo=UTC)),
            ("order_id", UUID(int=99)),
            ("metadata", {}),
        ],
    )
    def test_fields_cannot_be_reassigned(self, field: str, value: object) -> None:
        event = make_event()
        with pytest.raises(FrozenInstanceError):
            setattr(event, field, value)

    def test_metadata_cannot_be_mutated_in_place(self) -> None:
        event = make_event(metadata={"a": 1})
        with pytest.raises(TypeError):
            event.metadata["a"] = 2  # type: ignore[index]
        with pytest.raises(TypeError):
            event.metadata["b"] = 2  # type: ignore[index]
        assert event.metadata == {"a": 1}

    def test_nested_metadata_is_frozen_too(self) -> None:
        event = make_event(metadata={"outer": {"inner": [1, {"deep": "x"}]}})
        outer = event.metadata["outer"]
        assert isinstance(outer, Mapping)
        with pytest.raises(TypeError):
            outer["inner"] = []  # type: ignore[index]
        inner = outer["inner"]
        assert isinstance(inner, tuple)
        assert isinstance(inner[1], Mapping)
        with pytest.raises(TypeError):
            inner[1]["deep"] = "y"

    def test_metadata_is_a_copy_of_the_callers_data(self) -> None:
        source: dict[str, Any] = {"a": {"b": 1}, "c": [1, 2]}
        event = make_event(metadata=source)
        source["a"]["b"] = 2
        source["c"].append(3)
        source["new"] = True
        assert event.metadata == {"a": {"b": 1}, "c": (1, 2)}


class TestConsistency:
    def test_order_created_event(self) -> None:
        event = make_event(
            event_type=T.ORDER_CREATED, previous_state=None, new_state=S.CREATED, drone_id=None
        )
        assert event.previous_state is None

    @pytest.mark.parametrize(
        ("previous", "new"),
        [(S.CREATED, S.CREATED), (None, S.ASSIGNED), (S.CREATED, None), (None, None)],
    )
    def test_order_created_must_go_from_nothing_to_created(
        self, previous: OrderStatus | None, new: OrderStatus | None
    ) -> None:
        with pytest.raises(AuditEventValidationError):
            make_event(event_type=T.ORDER_CREATED, previous_state=previous, new_state=new)

    @pytest.mark.parametrize(
        ("previous", "new"),
        [(None, S.ASSIGNED), (S.CREATED, None), (None, None)],
    )
    def test_status_change_needs_both_states(
        self, previous: OrderStatus | None, new: OrderStatus | None
    ) -> None:
        with pytest.raises(AuditEventValidationError, match="previous_state and new_state"):
            make_event(previous_state=previous, new_state=new)

    def test_status_change_must_actually_change_the_state(self) -> None:
        with pytest.raises(AuditEventValidationError, match="change the state"):
            make_event(previous_state=S.ASSIGNED, new_state=S.ASSIGNED)

    def test_timestamp_must_be_timezone_aware(self) -> None:
        with pytest.raises(AuditEventValidationError, match="timezone-aware"):
            make_event(occurred_at=datetime(2026, 11, 11, 9, 0))


class TestMetadataValidation:
    @pytest.mark.parametrize(
        "metadata",
        [
            {},
            {"s": "x", "i": 1, "f": 1.5, "b": True, "n": None},
            {"list": [1, "two", None], "tuple": (1, 2), "map": {"k": [{"x": 1}]}},
        ],
    )
    def test_json_compatible_values_are_accepted(self, metadata: dict[str, Any]) -> None:
        event = make_event(metadata=metadata)
        assert set(event.metadata) == set(metadata)

    @pytest.mark.parametrize(
        "bad",
        [UUID(int=1), NOW, {1, 2}, object(), b"bytes", {"nested": {"x": UUID(int=1)}}, [NOW]],
    )
    def test_values_that_are_not_json_compatible_are_rejected(self, bad: object) -> None:
        with pytest.raises(AuditEventValidationError, match="unsupported type"):
            make_event(metadata={"bad": bad})

    @pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
    def test_non_finite_numbers_are_rejected(self, bad: float) -> None:
        with pytest.raises(AuditEventValidationError, match="finite"):
            make_event(metadata={"x": bad})

    def test_non_string_keys_are_rejected(self) -> None:
        with pytest.raises(AuditEventValidationError, match="non-string key"):
            make_event(metadata={1: "x"})

    def test_metadata_must_be_a_mapping(self) -> None:
        with pytest.raises(AuditEventValidationError, match="mapping"):
            make_event(metadata=["not", "a", "mapping"])

    def test_error_names_the_offending_path(self) -> None:
        with pytest.raises(AuditEventValidationError, match=r"metadata\.a\.b\[1\]"):
            make_event(metadata={"a": {"b": [1, object()]}})
