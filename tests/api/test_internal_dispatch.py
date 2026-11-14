"""Internal dispatch and flight-command endpoints (Rules 1 and 2 through HTTP)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.dispatch.link import CommandRejectedError, NoAcknowledgementError
from tests.api.conftest import place_order, register_drone
from tests.dispatch_rig import HookedLink, ScriptedCompliance

MISSING = "00000000-0000-0000-0000-0000000000ff"


async def dispatched(client: httpx.AsyncClient, *, start: bool = True) -> dict[str, Any]:
    await register_drone(client)
    order_id = await place_order(client)
    response = await client.post(f"/internal/orders/{order_id}/dispatch", params={"start": start})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


class TestDispatch:
    async def test_it_assigns_and_starts(self, client: httpx.AsyncClient, link: HookedLink) -> None:
        body = await dispatched(client)

        assert body["outcome"] == "started"
        assert body["succeeded"] is True
        assert body["order"]["status"] == "assigned"
        assert body["order"]["drone_id"] == body["drone"]["id"]
        assert body["drone"]["status"] == "on_mission"
        assert body["flight"]["status"] == "started"
        assert body["rejections"] == {}
        assert [op for op, _ in link.commands] == ["upload", "start"]

    async def test_the_flight_shows_the_mission_and_permission(
        self, client: httpx.AsyncClient
    ) -> None:
        flight = (await dispatched(client))["flight"]

        assert [w["kind"] for w in flight["mission"]["waypoints"]] == [
            "takeoff",
            "pickup",
            "dropoff",
            "return",
            "land",
        ]
        assert flight["acknowledged_checksum"] == flight["mission"]["checksum"]
        assert flight["permission"]["permission_id"].startswith("MOCK-")
        assert flight["denial"] is None
        assert flight["started_at"] is not None

    async def test_start_false_stages_the_mission(
        self, client: httpx.AsyncClient, link: HookedLink
    ) -> None:
        body = await dispatched(client, start=False)

        assert body["outcome"] == "ready"
        assert body["succeeded"] is True
        assert body["flight"]["status"] == "mission_uploaded"
        assert body["drone"]["status"] == "assigned"
        assert [op for op, _ in link.commands] == ["upload"]

    async def test_a_staged_flight_is_started_by_a_command(
        self, client: httpx.AsyncClient, link: HookedLink
    ) -> None:
        flight_id = (await dispatched(client, start=False))["flight"]["id"]

        response = await client.post(f"/internal/flights/{flight_id}/start")

        assert response.status_code == 200
        assert response.json()["status"] == "started"
        assert [op for op, _ in link.commands] == ["upload", "start"]

    async def test_no_drone_is_a_refusal_not_an_error(self, client: httpx.AsyncClient) -> None:
        order_id = await place_order(client)

        response = await client.post(f"/internal/orders/{order_id}/dispatch")

        assert response.status_code == 200
        body = response.json()
        assert body["outcome"] == "no_drone_available"
        assert body["succeeded"] is False
        assert body["order"]["status"] == "created"
        assert body["flight"] is None

    async def test_it_explains_why_a_drone_was_rejected(self, client: httpx.AsyncClient) -> None:
        drone = await register_drone(client, max_payload_kg=0.5)
        order_id = await place_order(client)

        body = (await client.post(f"/internal/orders/{order_id}/dispatch")).json()

        assert body["rejections"] == {drone["id"]: ["payload_too_heavy"]}

    async def test_a_denied_permission(
        self, client: httpx.AsyncClient, compliance: ScriptedCompliance, link: HookedLink
    ) -> None:
        compliance.close_airspace("closed for a parade")
        await register_drone(client)
        order_id = await place_order(client)

        body = (await client.post(f"/internal/orders/{order_id}/dispatch")).json()

        assert body["outcome"] == "permission_denied"
        assert body["detail"] == "closed for a parade"
        assert body["order"]["status"] == "created"
        assert body["flight"]["status"] == "permission_denied"
        assert body["flight"]["denial"] == {
            "code": "airspace_closed",
            "reason": "closed for a parade",
        }
        assert body["drone"]["status"] == "available"
        assert link.commands == []

    async def test_a_failed_upload(self, client: httpx.AsyncClient, link: HookedLink) -> None:
        link.set_failure("upload", NoAcknowledgementError("link down"))
        await register_drone(client)
        order_id = await place_order(client)

        body = (await client.post(f"/internal/orders/{order_id}/dispatch")).json()

        assert body["outcome"] == "upload_failed"
        assert body["order"]["status"] == "created"
        assert body["flight"]["status"] == "upload_failed"
        assert body["drone"]["status"] == "available"

    async def test_a_dispatched_order_can_be_retried_after_a_refusal(
        self, client: httpx.AsyncClient, compliance: ScriptedCompliance
    ) -> None:
        compliance.set_outage(True)
        await register_drone(client)
        order_id = await place_order(client)
        first = (await client.post(f"/internal/orders/{order_id}/dispatch")).json()
        assert first["outcome"] == "permission_denied"
        assert first["flight"]["denial"]["code"] == "provider_unavailable"
        compliance.set_outage(False)

        second = (await client.post(f"/internal/orders/{order_id}/dispatch")).json()

        assert second["outcome"] == "started"
        flights = (await client.get(f"/internal/orders/{order_id}/flights")).json()
        assert [f["status"] for f in flights] == ["permission_denied", "started"]

    async def test_an_unknown_order_is_not_found(self, client: httpx.AsyncClient) -> None:
        response = await client.post(f"/internal/orders/{MISSING}/dispatch")

        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    async def test_dispatching_twice_is_a_conflict(self, client: httpx.AsyncClient) -> None:
        await register_drone(client)
        order_id = await place_order(client)
        await client.post(f"/internal/orders/{order_id}/dispatch")

        response = await client.post(f"/internal/orders/{order_id}/dispatch")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"

    async def test_the_flights_of_an_order(self, client: httpx.AsyncClient) -> None:
        body = await dispatched(client)

        flights = (await client.get(f"/internal/orders/{body['order']['id']}/flights")).json()

        assert [f["id"] for f in flights] == [body["flight"]["id"]]

    async def test_the_flights_of_an_unknown_order(self, client: httpx.AsyncClient) -> None:
        assert (await client.get(f"/internal/orders/{MISSING}/flights")).status_code == 404

    async def test_get_flight(self, client: httpx.AsyncClient) -> None:
        flight = (await dispatched(client))["flight"]

        fetched = await client.get(f"/internal/flights/{flight['id']}")

        assert fetched.status_code == 200
        assert fetched.json() == flight

    async def test_an_unknown_flight_is_not_found(self, client: httpx.AsyncClient) -> None:
        assert (await client.get(f"/internal/flights/{MISSING}")).status_code == 404


class TestFlightCommands:
    async def test_pause_and_resume(self, client: httpx.AsyncClient, link: HookedLink) -> None:
        flight_id = (await dispatched(client))["flight"]["id"]

        paused = await client.post(f"/internal/flights/{flight_id}/pause")
        resumed = await client.post(f"/internal/flights/{flight_id}/resume")

        assert paused.json()["status"] == "paused"
        assert resumed.json()["status"] == "started"
        assert [op for op, _ in link.commands] == ["upload", "start", "pause", "start"]

    async def test_return_to_home_with_a_reason(
        self, client: httpx.AsyncClient, link: HookedLink
    ) -> None:
        body = await dispatched(client)
        flight_id = body["flight"]["id"]

        response = await client.post(
            f"/internal/flights/{flight_id}/return-to-home", json={"reason": "storm warning"}
        )

        assert response.status_code == 200
        assert response.json()["status"] == "aborted"
        assert "storm warning" in response.json()["failure_reason"]
        drone = (await client.get(f"/internal/drones/{body['drone']['id']}")).json()
        assert drone["status"] == "returning"
        assert link.commands[-1][0] == "return_to_home"

    async def test_land_without_a_body(self, client: httpx.AsyncClient, link: HookedLink) -> None:
        body = await dispatched(client)

        response = await client.post(f"/internal/flights/{body['flight']['id']}/land")

        assert response.status_code == 200
        assert response.json()["status"] == "aborted"
        drone = (await client.get(f"/internal/drones/{body['drone']['id']}")).json()
        assert drone["status"] == "offline"

    @pytest.mark.parametrize("command", ["return-to-home", "land"])
    @pytest.mark.parametrize(
        "steering",
        [
            {"latitude": 12.9, "longitude": 77.5},
            {"altitude_m": 120},
            {"heading": 90},
            {"speed": 30},
            {"waypoints": [{"latitude": 1, "longitude": 1}]},
            {"status": "completed"},
        ],
    )
    async def test_a_command_cannot_carry_anything_but_a_reason(
        self, client: httpx.AsyncClient, link: HookedLink, command: str, steering: dict[str, Any]
    ) -> None:
        """Rule 1: no way to hand the drone a position, heading, speed or status."""
        flight_id = (await dispatched(client))["flight"]["id"]
        sent = list(link.commands)

        response = await client.post(f"/internal/flights/{flight_id}/{command}", json=steering)

        assert response.status_code == 422
        assert link.commands == sent  # nothing reached the drone
        flight = (await client.get(f"/internal/flights/{flight_id}")).json()
        assert flight["status"] == "started"

    @pytest.mark.parametrize("command", ["start", "pause", "resume", "return-to-home", "land"])
    async def test_an_unknown_flight_is_not_found(
        self, client: httpx.AsyncClient, command: str
    ) -> None:
        response = await client.post(f"/internal/flights/{MISSING}/{command}")

        assert response.status_code == 404

    @pytest.mark.parametrize("command", ["start", "resume"])
    async def test_a_command_the_flight_state_does_not_allow_is_a_conflict(
        self, client: httpx.AsyncClient, command: str
    ) -> None:
        flight_id = (await dispatched(client))["flight"]["id"]  # already started

        response = await client.post(f"/internal/flights/{flight_id}/{command}")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"

    @pytest.mark.parametrize("command", ["pause", "return-to-home", "land"])
    async def test_commands_on_a_finished_flight_are_a_conflict(
        self, client: httpx.AsyncClient, command: str
    ) -> None:
        flight_id = (await dispatched(client))["flight"]["id"]
        await client.post(f"/internal/flights/{flight_id}/land")

        response = await client.post(f"/internal/flights/{flight_id}/{command}")

        assert response.status_code == 409

    @pytest.mark.parametrize("command", ["pause", "return-to-home", "land"])
    async def test_an_unconfirmed_command_is_a_503_and_records_nothing(
        self, client: httpx.AsyncClient, link: HookedLink, command: str
    ) -> None:
        flight_id = (await dispatched(client))["flight"]["id"]
        operation = {"pause": "pause", "return-to-home": "return_to_home", "land": "land"}[command]
        link.set_failure(operation, NoAcknowledgementError("no answer"))  # type: ignore[arg-type]

        response = await client.post(f"/internal/flights/{flight_id}/{command}")

        assert response.status_code == 503
        assert response.json()["error"]["code"] == "command_not_confirmed"
        flight = (await client.get(f"/internal/flights/{flight_id}")).json()
        assert flight["status"] == "started"


class TestRule2OverHttp:
    async def test_a_revoked_permission_blocks_a_staged_start_with_409(
        self, client: httpx.AsyncClient, compliance: ScriptedCompliance, link: HookedLink
    ) -> None:
        flight = (await dispatched(client, start=False))["flight"]
        compliance.revoke(flight["permission"]["permission_id"])

        response = await client.post(f"/internal/flights/{flight['id']}/start")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "permission_refused"
        assert [op for op, _ in link.commands] == ["upload"]  # never started
        assert (await client.get(f"/internal/flights/{flight['id']}")).json()["status"] == (
            "mission_uploaded"
        )

    async def test_an_outage_at_the_gate_fails_closed(
        self, client: httpx.AsyncClient, compliance: ScriptedCompliance, link: HookedLink
    ) -> None:
        flight = (await dispatched(client, start=False))["flight"]
        compliance.set_outage(True)

        response = await client.post(f"/internal/flights/{flight['id']}/start")

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "permission_refused"
        assert [op for op, _ in link.commands] == ["upload"]

    async def test_a_revoked_permission_never_blocks_a_recall(
        self, client: httpx.AsyncClient, compliance: ScriptedCompliance
    ) -> None:
        flight = (await dispatched(client))["flight"]
        compliance.revoke(flight["permission"]["permission_id"])
        compliance.set_outage(True)

        response = await client.post(f"/internal/flights/{flight['id']}/return-to-home")

        assert response.status_code == 200
        assert response.json()["status"] == "aborted"

    async def test_a_dispatch_whose_gate_refuses_reports_start_failed(
        self, client: httpx.AsyncClient, compliance: ScriptedCompliance, link: HookedLink
    ) -> None:
        compliance.on_validate = lambda n, pid: compliance.revoke(pid) if n == 2 else None
        await register_drone(client)
        order_id = await place_order(client)

        body = (await client.post(f"/internal/orders/{order_id}/dispatch")).json()

        assert body["outcome"] == "start_failed"
        assert body["order"]["status"] == "failed"
        assert body["flight"]["status"] == "cancelled"
        assert [op for op, _ in link.commands] == ["upload"]

    async def test_a_refused_start_command_is_a_502_after_cleanup(
        self, client: httpx.AsyncClient, link: HookedLink
    ) -> None:
        body = await dispatched(client, start=False)
        link.set_failure("start", CommandRejectedError("motors not armed"))

        response = await client.post(f"/internal/flights/{body['flight']['id']}/start")

        assert response.status_code == 502
        assert response.json()["error"]["code"] == "start_failed"
        flight = (await client.get(f"/internal/flights/{body['flight']['id']}")).json()
        assert flight["status"] == "aborted"
        order = (await client.get(f"/internal/orders/{body['order']['id']}")).json()
        assert order["status"] == "failed"


class TestIdsAreValidated:
    @pytest.mark.parametrize(
        "path",
        [
            "/internal/flights/not-a-uuid",
            "/internal/flights/not-a-uuid/start",
            "/internal/orders/not-a-uuid/dispatch",
            "/internal/drones/not-a-uuid",
        ],
    )
    async def test_a_malformed_id_is_rejected(self, client: httpx.AsyncClient, path: str) -> None:
        method = "get" if path.count("/") == 3 else "post"

        response = await client.request(method, path)

        assert response.status_code == 422
