"""Architecture test for Rule 3 and the layering that protects it.

Layers and what each may import (besides the standard library):

====================  ==============================================================
``app/domain``        only ``app.domain`` (see ``test_domain_purity.py``)
``app/adapters``      ``pydantic``, ``app.domain``, ``app.adapters``
``app/services``      ``structlog``, ``app.domain``, ``app.services``
``app/db``            SQLAlchemy/GeoAlchemy2/Alembic, ``app.domain``, ``app.services``,
                      ``app.db``, ``app.config``
``app/compliance``    ``app.domain``, ``app.compliance``
``app/dispatch``      structlog, ``app.domain``, ``app.compliance``, ``app.services``,
                      ``app.dispatch``
``app/api``           FastAPI/Starlette/Pydantic/structlog, ``app.domain``,
                      ``app.services``, ``app.adapters``, ``app.api``, and from dispatch
                      only ``app.dispatch.service`` and ``app.dispatch.exceptions``
====================  ==============================================================

So partner formats (adapters) never reach core code; core never imports the HTTP layer or
the adapters; and HTTP handlers never touch the database layer or the drone link (Rule 1:
only the dispatch service talks to drones). ``app/main.py`` is the composition root and may
import across layers.

A second check keeps partner *names* out of everything except ``app/adapters``.
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP_DIR = ROOT / "app"
STDLIB = set(sys.stdlib_module_names)


@dataclass(frozen=True)
class LayerRule:
    third_party: frozenset[str]
    app_packages: frozenset[str]


RULES: dict[str, LayerRule] = {
    "app/adapters": LayerRule(frozenset({"pydantic"}), frozenset({"app.domain", "app.adapters"})),
    "app/services": LayerRule(frozenset({"structlog"}), frozenset({"app.domain", "app.services"})),
    "app/db": LayerRule(
        frozenset({"sqlalchemy", "geoalchemy2", "alembic"}),
        frozenset({"app.domain", "app.services", "app.db", "app.config"}),
    ),
    # The compliance interface and its mock know only the domain.
    "app/compliance": LayerRule(frozenset(), frozenset({"app.domain", "app.compliance"})),
    # Dispatch logic: the domain, the compliance interface and the service ports. Never the
    # database layer, the HTTP layer or the partner adapters.
    "app/dispatch": LayerRule(
        frozenset({"structlog"}),
        frozenset({"app.domain", "app.compliance", "app.services", "app.dispatch"}),
    ),
    # Handlers may call the dispatch service and render its errors, but never import the
    # drone link: the service is the only thing that may talk to a drone (Rule 1).
    "app/api": LayerRule(
        frozenset({"fastapi", "starlette", "pydantic", "structlog"}),
        frozenset(
            {
                "app.domain",
                "app.services",
                "app.adapters",
                "app.api",
                "app.dispatch.service",
                "app.dispatch.exceptions",
            }
        ),
    ),
}


def _imported_modules(source: str) -> list[str]:
    modules: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            modules.append(node.module or "")
    return modules


def find_boundary_violations(source: str, rule: LayerRule) -> list[str]:
    violations: list[str] = []
    for module in _imported_modules(source):
        top = module.split(".")[0]
        if top == "app":
            allowed = any(module == p or module.startswith(p + ".") for p in rule.app_packages)
            if not allowed:
                violations.append(module)
        elif top not in STDLIB and top not in rule.third_party:
            violations.append(module)
    return violations


def _files(layer: str) -> list[Path]:
    return sorted((ROOT / layer).rglob("*.py"))


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


CASES = [(layer, path) for layer in RULES for path in _files(layer)]


@pytest.mark.parametrize(("layer", "path"), CASES, ids=[_relative(p) for _, p in CASES])
def test_module_respects_its_layer(layer: str, path: Path) -> None:
    assert find_boundary_violations(path.read_text(), RULES[layer]) == []


@pytest.mark.parametrize("layer", RULES)
def test_every_layer_has_modules_to_check(layer: str) -> None:
    assert len(_files(layer)) >= 2, f"{layer} should contain modules; is the scan pointed right?"


def test_partner_names_appear_only_inside_the_adapters() -> None:
    pattern = re.compile(r"swiggy|zomato", re.IGNORECASE)
    leaks = [
        _relative(path)
        for path in sorted(APP_DIR.rglob("*.py"))
        if not _relative(path).startswith("app/adapters/") and pattern.search(path.read_text())
    ]
    assert leaks == []


def test_the_adapters_do_mention_their_partners() -> None:
    """Guards the check above against matching nothing because of a wrong path."""
    pattern = re.compile(r"swiggy|zomato", re.IGNORECASE)
    assert any(pattern.search(p.read_text()) for p in _files("app/adapters"))


class TestTheDetectorItself:
    ADAPTERS = RULES["app/adapters"]
    API = RULES["app/api"]
    SERVICES = RULES["app/services"]

    @pytest.mark.parametrize(
        "source",
        [
            "from app.services.order_service import OrderService",  # adapters -> services
            "from app.db.session import create_engine",  # adapters -> db
            "from app.api.deps import get_order_service",  # adapters -> api
            "import sqlalchemy",
            "from fastapi import APIRouter",
            "import httpx",
            "import redis",
            "def f():\n    from app.db.models import OrderRecord",
        ],
    )
    def test_flags_what_adapters_may_not_import(self, source: str) -> None:
        assert find_boundary_violations(source, self.ADAPTERS) != []

    @pytest.mark.parametrize(
        "source",
        [
            "from app.db.uow import SqlAlchemyUnitOfWork",  # api -> db
            "from app.db.repositories.orders import SqlAlchemyOrderStore",
            "import sqlalchemy",
            "import asyncpg",
            "import app.db",
        ],
    )
    def test_flags_the_api_reaching_for_the_database(self, source: str) -> None:
        assert find_boundary_violations(source, self.API) != []

    @pytest.mark.parametrize(
        "source",
        [
            "from app.dispatch.link import DroneLink",
            "from app.dispatch.in_memory_link import InMemoryDroneLink",
            "from app.dispatch.mission import plan_mission",
            "from app.dispatch.assignment import select_drone",
            "import app.dispatch.link",
            "from app.compliance.mock_digital_sky import MockDigitalSky",
        ],
    )
    def test_flags_the_api_reaching_the_drone_link_or_dispatch_internals(self, source: str) -> None:
        """Rule 1: a route handler must not be able to talk to a drone directly."""
        assert find_boundary_violations(source, self.API) != []

    @pytest.mark.parametrize(
        "source",
        [
            "from app.dispatch.service import DispatchService",
            "from app.dispatch.exceptions import FlightNotFoundError",
        ],
    )
    def test_allows_the_api_to_use_the_dispatch_service_and_its_errors(self, source: str) -> None:
        assert find_boundary_violations(source, self.API) == []

    @pytest.mark.parametrize(
        "source",
        [
            "from app.adapters.swiggy import SwiggyAdapter",  # core -> adapters
            "from app.api.deps import OrderServiceDep",  # core -> api
            "import fastapi",
            "from app.db.session import create_engine",  # services -> db
        ],
    )
    def test_flags_core_importing_the_edges(self, source: str) -> None:
        assert find_boundary_violations(source, self.SERVICES) != []

    @pytest.mark.parametrize(
        "source",
        [
            "from __future__ import annotations\nimport json\nfrom typing import Any",
            "from pydantic import BaseModel",
            "from app.domain.models import Order",
            "from app.adapters.base import ClientAdapter",
            "from .common import PartnerModel",
        ],
    )
    def test_allows_legitimate_adapter_imports(self, source: str) -> None:
        assert find_boundary_violations(source, self.ADAPTERS) == []

    @pytest.mark.parametrize(
        "source",
        [
            "from app.db.session import create_engine",
            "from app.db.uow import SqlAlchemyUnitOfWork",
            "from app.api.deps import OrderServiceDep",
            "from app.adapters.swiggy import SwiggyAdapter",
            "import sqlalchemy",
            "import fastapi",
            "import paho.mqtt.client",
            "from app.compliance.mock_digital_sky import MockDigitalSky\nimport httpx",
        ],
    )
    def test_flags_dispatch_reaching_the_edges(self, source: str) -> None:
        assert find_boundary_violations(source, RULES["app/dispatch"]) != []

    @pytest.mark.parametrize(
        "source",
        [
            "from app.services.order_service import OrderService",
            "from app.dispatch.assignment import select_drone",
            "import structlog",
            "from app.db.models import DroneRecord",
        ],
    )
    def test_flags_the_compliance_package_reaching_beyond_the_domain(self, source: str) -> None:
        assert find_boundary_violations(source, RULES["app/compliance"]) != []

    def test_allows_dispatch_to_use_the_domain_compliance_and_service_ports(self) -> None:
        source = (
            "from app.domain.models import Drone\n"
            "from app.compliance.interface import ComplianceProvider\n"
            "from app.services.ports import UnitOfWorkFactory\n"
            "import structlog\n"
        )
        assert find_boundary_violations(source, RULES["app/dispatch"]) == []

    def test_allows_the_api_to_use_services_adapters_and_fastapi(self) -> None:
        source = (
            "from fastapi import APIRouter\n"
            "from app.services.order_service import OrderService\n"
            "from app.adapters.registry import AdapterRegistry\n"
            "from app.domain.models import Order\n"
        )
        assert find_boundary_violations(source, self.API) == []
