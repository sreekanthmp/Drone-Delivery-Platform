"""Drone model and its status transitions."""

from __future__ import annotations

import itertools
import math
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.domain.exceptions import DroneValidationError, InvalidDroneTransitionError
from app.domain.models import ALLOWED_DRONE_TRANSITIONS, Drone, DroneStatus
from app.domain.models.drone import check_status_change
from tests.conftest import HOME, T0, build_drone

D = DroneStatus

# Written out by hand, not derived from the implementation.
ALLOWED_EDGES = {
    (D.AVAILABLE, D.ASSIGNED),
    (D.AVAILABLE, D.MAINTENANCE),
    (D.AVAILABLE, D.OFFLINE),
    (D.ASSIGNED, D.AVAILABLE),
    (D.ASSIGNED, D.ON_MISSION),
    (D.ASSIGNED, D.MAINTENANCE),
    (D.ASSIGNED, D.OFFLINE),
    (D.ON_MISSION, D.RETURNING),
    (D.ON_MISSION, D.AVAILABLE),
    (D.ON_MISSION, D.OFFLINE),
    (D.RETURNING, D.AVAILABLE),
    (D.RETURNING, D.OFFLINE),
    (D.MAINTENANCE, D.AVAILABLE),
    (D.MAINTENANCE, D.OFFLINE),
    (D.OFFLINE, D.AVAILABLE),
    (D.OFFLINE, D.MAINTENANCE),
}
PAIRS = list(itertools.product(DroneStatus, DroneStatus))


class TestValidation:
    def test_a_valid_drone(self) -> None:
        drone = build_drone()
        assert drone.is_available
        assert drone.home == HOME

    @pytest.mark.parametrize("name", ["", "   ", "\t"])
    def test_blank_names_are_rejected(self, name: str) -> None:
        with pytest.raises(DroneValidationError, match="name"):
            build_drone(name=name)

    @pytest.mark.parametrize("field", ["max_payload_kg", "max_range_km"])
    @pytest.mark.parametrize("value", [0, -1, math.nan, math.inf])
    def test_capacity_must_be_a_positive_number(self, field: str, value: float) -> None:
        with pytest.raises(DroneValidationError, match=field):
            build_drone(**{field: value})

    @pytest.mark.parametrize("value", [-0.1, 100.1, math.nan, math.inf])
    def test_battery_must_be_within_0_and_100(self, value: float) -> None:
        with pytest.raises(DroneValidationError, match="battery"):
            build_drone(battery_percent=value)

    @pytest.mark.parametrize("value", [0, 100])
    def test_battery_boundaries_are_accepted(self, value: float) -> None:
        assert build_drone(battery_percent=value).battery_percent == value

    @pytest.mark.parametrize("field", ["created_at", "updated_at"])
    def test_timestamps_must_be_timezone_aware(self, field: str) -> None:
        with pytest.raises(DroneValidationError, match=field):
            build_drone(**{field: datetime(2026, 11, 11, 9, 0)})

    def test_is_immutable(self) -> None:
        drone = build_drone()
        with pytest.raises(FrozenInstanceError):
            drone.status = D.OFFLINE  # type: ignore[misc]


class TestStatusTransitions:
    def test_the_table_is_exactly_the_hand_written_edges(self) -> None:
        actual = {(s, t) for s, targets in ALLOWED_DRONE_TRANSITIONS.items() for t in targets}
        assert actual == ALLOWED_EDGES
        assert len(ALLOWED_EDGES) == 16

    def test_every_status_has_a_row(self) -> None:
        assert set(ALLOWED_DRONE_TRANSITIONS) == set(DroneStatus)

    def test_the_table_cannot_be_modified(self) -> None:
        with pytest.raises(TypeError):
            ALLOWED_DRONE_TRANSITIONS[D.AVAILABLE] = frozenset()  # type: ignore[index]

    @pytest.mark.parametrize(("src", "dst"), PAIRS, ids=[f"{s}->{d}" for s, d in PAIRS])
    def test_with_status_follows_the_table(self, src: DroneStatus, dst: DroneStatus) -> None:
        drone = build_drone(status=src)
        later = T0 + timedelta(minutes=1)

        if (src, dst) in ALLOWED_EDGES:
            changed = drone.with_status(dst, now=later)
            assert changed.status is dst
            assert changed.updated_at == later
            assert drone.status is src, "the original is untouched"
        else:
            with pytest.raises(InvalidDroneTransitionError) as exc_info:
                drone.with_status(dst, now=later)
            assert (exc_info.value.current, exc_info.value.target) == (src, dst)

    def test_other_fields_are_preserved(self) -> None:
        drone = build_drone(battery_percent=55.5, name="alpha")
        changed = drone.with_status(D.ASSIGNED, now=T0)
        assert (changed.id, changed.name, changed.home, changed.battery_percent) == (
            drone.id,
            "alpha",
            drone.home,
            55.5,
        )

    def test_a_drone_in_a_non_available_status_is_not_available(self) -> None:
        assert not build_drone(status=D.ASSIGNED).is_available


class TestCheckStatusChange:
    def test_accepts_a_legal_change(self) -> None:
        before = build_drone()
        check_status_change(before, before.with_status(D.ASSIGNED, now=T0))

    def test_rejects_a_different_drone(self) -> None:
        before = build_drone()
        other = replace(
            before.with_status(D.ASSIGNED, now=T0),
            id=build_drone(id=__import__("uuid").UUID(int=9)).id,
        )
        with pytest.raises(DroneValidationError, match="same drone"):
            check_status_change(before, other)

    def test_rejects_an_illegal_edge(self) -> None:
        before = build_drone(status=D.OFFLINE)
        with pytest.raises(InvalidDroneTransitionError):
            check_status_change(before, replace(before, status=D.ON_MISSION))

    @pytest.mark.parametrize(
        ("field", "value"),
        [("name", "renamed"), ("battery_percent", 10.0), ("max_payload_kg", 99.0)],
    )
    def test_rejects_changes_to_anything_but_status_and_timestamp(
        self, field: str, value: Any
    ) -> None:
        before = build_drone()
        after = replace(before.with_status(D.ASSIGNED, now=T0), **{field: value})
        with pytest.raises(DroneValidationError, match="only change status"):
            check_status_change(before, after)

    def test_rehydrate_builds_a_drone_in_any_status(self) -> None:
        for status in DroneStatus:
            assert isinstance(build_drone(status=status), Drone)
