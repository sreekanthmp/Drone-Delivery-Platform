"""The MQTT wire contract: topics and messages (Rule 1 is visible in their shape)."""

from __future__ import annotations

import json
from typing import Any, get_args
from uuid import UUID

import pytest

from app.domain.exceptions import MissionValidationError
from app.domain.flight_state_machine import StartAuthorization
from app.protocol import messages as m
from app.protocol import topics
from tests.conftest import DRONE_ID, FLIGHT_ID, T0, build_mission

REPLY = "platform/acks/abc123"
COMMAND_ID = UUID(int=0xC1)


def base(**extra: Any) -> dict[str, Any]:
    return {
        "command_id": str(COMMAND_ID),
        "drone_id": str(DRONE_ID),
        "sent_at": T0.isoformat(),
        "reply_to": REPLY,
        **extra,
    }


def authorization() -> StartAuthorization:
    return StartAuthorization(
        flight_id=FLIGHT_ID,
        drone_id=DRONE_ID,
        permission_id="PERM-1",
        mission_checksum=build_mission().checksum,
        authorized_at=T0,
        valid_until=T0.replace(hour=10),
    )


class TestTopics:
    def test_topic_names(self) -> None:
        assert topics.command_topic(DRONE_ID) == f"drones/{DRONE_ID}/commands"
        assert topics.event_topic(DRONE_ID) == f"drones/{DRONE_ID}/events"
        assert topics.ack_topic("api-1") == "platform/acks/api-1"
        assert topics.EVENTS_FILTER == "drones/+/events"

    @pytest.mark.parametrize("bad", ["", "a/b", "a+b", "a#", "a b", "x" * 65, "é"])
    def test_an_instance_id_cannot_smuggle_a_wildcard_or_a_path(self, bad: str) -> None:
        with pytest.raises(ValueError, match="invalid instance id"):
            topics.ack_topic(bad)

    def test_an_event_topic_round_trips(self) -> None:
        assert topics.parse_event_topic(topics.event_topic(DRONE_ID)) == DRONE_ID

    @pytest.mark.parametrize(
        "topic",
        [
            "drones/not-a-uuid/events",
            f"drones/{DRONE_ID}/commands",
            f"drones/{DRONE_ID}/events/extra",
            f"other/{DRONE_ID}/events",
            "drones/+/events",
            "drones/#",
            "",
            f"drones/{'g' * 36}/events",
        ],
    )
    def test_anything_else_is_not_an_event_topic(self, topic: str) -> None:
        assert topics.parse_event_topic(topic) is None

    def test_ack_topic_recognition(self) -> None:
        assert topics.is_ack_topic("platform/acks/api-1")
        assert not topics.is_ack_topic("platform/acks/")
        assert not topics.is_ack_topic("platform/acks/a/b")
        assert not topics.is_ack_topic(topics.event_topic(DRONE_ID))


class TestRule1InTheShapeOfTheContract:
    def test_there_are_exactly_five_command_types(self) -> None:
        assert (
            frozenset(
                {"upload_mission", "start_mission", "pause_mission", "return_to_home", "land"}
            )
            == m.COMMAND_TYPES
        )

    def test_the_command_union_has_one_class_per_type(self) -> None:
        union = get_args(get_args(m.Command)[0])
        assert {cls.model_fields["type"].default for cls in union} == m.COMMAND_TYPES

    STEERING = ("lat", "lon", "alt", "heading", "yaw", "speed", "velocity", "position", "target")

    @pytest.mark.parametrize("cls", [m.PauseMission, m.ReturnToHome, m.Land])
    def test_the_stop_commands_carry_nothing_but_their_identity(
        self, cls: type[m._Command]
    ) -> None:
        assert set(cls.model_fields) == {"command_id", "drone_id", "sent_at", "reply_to", "type"}

    @pytest.mark.parametrize("cls", [m.PauseMission, m.ReturnToHome, m.Land, m.StartMission])
    def test_no_command_field_could_carry_a_position_or_a_speed(
        self, cls: type[m._Command]
    ) -> None:
        for name in cls.model_fields:
            assert not any(word in name for word in self.STEERING), name

    def test_a_command_with_an_extra_steering_field_is_rejected(self) -> None:
        payload = json.dumps(base(type="return_to_home", latitude=12.0)).encode()

        with pytest.raises(m.ProtocolError):
            m.decode_command(payload)

    def test_an_unknown_command_type_is_rejected(self) -> None:
        with pytest.raises(m.ProtocolError, match="not a valid command"):
            m.decode_command(json.dumps(base(type="goto")).encode())


class TestCommands:
    def test_every_command_round_trips(self) -> None:
        mission = m.WireMission.from_domain(build_mission())
        commands: list[m._Command] = [
            m.UploadMission(**base(), mission=mission),
            m.StartMission(
                **base(), authorization=m.WireAuthorization.from_domain(authorization())
            ),
            m.PauseMission(**base()),
            m.ReturnToHome(**base()),
            m.Land(**base()),
        ]

        for command in commands:
            assert m.decode_command(m.encode(command)) == command

    def test_a_mission_survives_the_wire_with_its_checksum(self) -> None:
        mission = build_mission()

        wire = m.WireMission.from_domain(mission)
        rebuilt = m.decode_command(m.encode(m.UploadMission(**base(), mission=wire)))
        assert isinstance(rebuilt, m.UploadMission)

        assert rebuilt.mission.to_domain() == mission
        assert rebuilt.mission.to_domain().checksum == mission.checksum == rebuilt.mission.checksum

    def test_a_tampered_mission_no_longer_matches_its_checksum(self) -> None:
        wire = m.WireMission.from_domain(build_mission())
        tampered = wire.model_copy(update={"cruise_speed_mps": 99.0})

        assert tampered.to_domain().checksum != tampered.checksum

    def test_a_structurally_invalid_mission_is_refused_when_rebuilt(self) -> None:
        wire = m.WireMission.from_domain(build_mission())
        broken = wire.model_copy(update={"waypoints": wire.waypoints[:1]})

        with pytest.raises(MissionValidationError):
            broken.to_domain()

    def test_the_authorisation_carries_the_gate_facts(self) -> None:
        wire = m.WireAuthorization.from_domain(authorization())

        assert wire.flight_id == FLIGHT_ID
        assert wire.drone_id == DRONE_ID
        assert wire.permission_id == "PERM-1"
        assert wire.mission_checksum == build_mission().checksum
        assert wire.valid_until > wire.authorized_at

    @pytest.mark.parametrize(
        "payload",
        [
            b"",
            b"not json",
            b"[]",
            b"{}",
            json.dumps(base()).encode(),  # no type
            json.dumps(base(type="land", sent_at="2026-11-11T09:00:00")).encode(),  # naive time
            json.dumps({**base(type="land"), "command_id": "nope"}).encode(),
        ],
    )
    def test_malformed_commands_are_protocol_errors_not_crashes(self, payload: bytes) -> None:
        with pytest.raises(m.ProtocolError):
            m.decode_command(payload)


class TestAcks:
    def ack(self, **extra: Any) -> dict[str, Any]:
        return {
            "command_id": str(COMMAND_ID),
            "drone_id": str(DRONE_ID),
            "status": "accepted",
            "acked_at": T0.isoformat(),
            **extra,
        }

    def test_an_ack_round_trips(self) -> None:
        ack = m.AckMessage.model_validate(self.ack(mission_checksum="abc"))

        assert m.decode_ack(m.encode(ack)) == ack
        assert ack.status is m.AckStatus.ACCEPTED

    def test_a_rejection_can_carry_a_reason(self) -> None:
        ack = m.decode_ack(json.dumps(self.ack(status="rejected", reason="not armed")).encode())

        assert ack.status is m.AckStatus.REJECTED
        assert ack.reason == "not armed"

    @pytest.mark.parametrize(
        "payload",
        [
            b"",
            b"{}",
            json.dumps({"status": "accepted"}).encode(),
            json.dumps({"command_id": str(COMMAND_ID), "drone_id": str(DRONE_ID)}).encode(),
            json.dumps(
                {
                    **{
                        "command_id": str(COMMAND_ID),
                        "drone_id": str(DRONE_ID),
                        "acked_at": T0.isoformat(),
                    },
                    "status": "maybe",
                }
            ).encode(),
        ],
    )
    def test_malformed_acks_are_protocol_errors(self, payload: bytes) -> None:
        with pytest.raises(m.ProtocolError):
            m.decode_ack(payload)

    def test_an_overlong_reason_is_refused(self) -> None:
        with pytest.raises(m.ProtocolError):
            m.decode_ack(json.dumps(self.ack(status="rejected", reason="x" * 501)).encode())


class TestEvents:
    def event(self, **extra: Any) -> dict[str, Any]:
        return {
            "event_id": str(UUID(int=0xE1)),
            "flight_id": str(FLIGHT_ID),
            "seq": 1,
            "type": "package_picked_up",
            "occurred_at": T0.isoformat(),
            **extra,
        }

    def test_the_event_vocabulary(self) -> None:
        assert {e.value for e in m.EventType} == {
            "package_picked_up",
            "departed_pickup",
            "arriving_at_dropoff",
            "package_delivered",
            "mission_completed",
            "docked",
            "fault",
        }

    def test_an_event_round_trips(self) -> None:
        event = m.DroneEvent.model_validate(self.event(detail={"code": "GPS_LOSS"}))

        assert m.decode_event(m.encode(event)) == event

    @pytest.mark.parametrize("seq", [0, -1])
    def test_the_sequence_starts_at_one(self, seq: int) -> None:
        with pytest.raises(m.ProtocolError):
            m.decode_event(json.dumps(self.event(seq=seq)).encode())

    @pytest.mark.parametrize(
        "payload",
        [
            b"",
            b"null",
            json.dumps({"seq": 1}).encode(),
            json.dumps({"event_id": "x"}).encode(),
        ],
    )
    def test_malformed_events_are_protocol_errors(self, payload: bytes) -> None:
        with pytest.raises(m.ProtocolError):
            m.decode_event(payload)

    def test_an_unknown_event_type_is_refused(self) -> None:
        with pytest.raises(m.ProtocolError):
            m.decode_event(json.dumps(self.event(type="teleported")).encode())

    def test_a_naive_timestamp_is_refused(self) -> None:
        with pytest.raises(m.ProtocolError):
            m.decode_event(json.dumps(self.event(occurred_at="2026-11-11T09:00:00")).encode())

    def test_an_unknown_field_is_refused(self) -> None:
        with pytest.raises(m.ProtocolError):
            m.decode_event(json.dumps(self.event(position={"lat": 1})).encode())


class TestTelemetry:
    def sample(self, **extra: Any) -> dict[str, Any]:
        return {
            "sent_at": T0.isoformat(),
            "latitude": 12.97,
            "longitude": 77.59,
            "altitude_m": 60.0,
            "battery_percent": 83.5,
            "phase": "flying",
            "flight_id": str(FLIGHT_ID),
            **extra,
        }

    def test_a_sample_round_trips(self) -> None:
        sample = m.Telemetry.model_validate(self.sample())

        assert m.decode_telemetry(m.encode(sample)) == sample

    def test_a_sample_on_the_ground_has_no_flight(self) -> None:
        raw = {k: v for k, v in self.sample(phase="idle").items() if k != "flight_id"}

        assert m.decode_telemetry(json.dumps(raw).encode()).flight_id is None

    def test_the_phases_are_the_drones_own_states(self) -> None:
        assert {p.value for p in m.DronePhase} == {
            "idle",
            "ready",
            "flying",
            "paused",
            "returning",
            "landed",
        }

    @pytest.mark.parametrize(
        "bad",
        [
            {"latitude": 91},
            {"latitude": -91},
            {"longitude": 181},
            {"altitude_m": -1},
            {"altitude_m": 30_000},
            {"battery_percent": -0.1},
            {"battery_percent": 100.1},
            {"phase": "hovering"},
            {"sent_at": "2026-11-11T09:00:00"},  # naive
            {"flight_id": "nope"},
            {"heading": 90},  # unknown field
        ],
    )
    def test_out_of_range_or_unknown_values_are_refused(self, bad: dict[str, Any]) -> None:
        with pytest.raises(m.ProtocolError, match="telemetry"):
            m.decode_telemetry(json.dumps(self.sample(**bad)).encode())

    @pytest.mark.parametrize("payload", [b"", b"garbage", b"[]", b"{}", b"null"])
    def test_garbage_is_a_protocol_error(self, payload: bytes) -> None:
        with pytest.raises(m.ProtocolError):
            m.decode_telemetry(payload)

    def test_the_topics(self) -> None:
        assert topics.telemetry_topic(DRONE_ID) == f"drones/{DRONE_ID}/telemetry"
        assert topics.TELEMETRY_FILTER == "drones/+/telemetry"
        assert topics.parse_telemetry_topic(topics.telemetry_topic(DRONE_ID)) == DRONE_ID
        assert topics.parse_telemetry_topic(topics.event_topic(DRONE_ID)) is None
        assert topics.parse_telemetry_topic("drones/+/telemetry") is None
