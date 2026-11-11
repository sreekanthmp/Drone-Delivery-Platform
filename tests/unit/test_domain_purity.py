"""Architecture test: the domain layer must stay free of I/O and infrastructure.

Every module under ``app/domain`` may import only the Python standard library
and other ``app.domain`` modules. That rules out SQLAlchemy, Redis, MQTT, HTTP
clients, FastAPI, Pydantic and any other ``app.*`` package (db, api, ...).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

DOMAIN_DIR = Path(__file__).resolve().parents[2] / "app" / "domain"
STDLIB = set(sys.stdlib_module_names)

# Stdlib modules that perform I/O. Importing them would defeat the purpose even
# though they are "standard library".
FORBIDDEN_STDLIB_IO = {
    "socket",
    "ssl",
    "http",
    "urllib",
    "ftplib",
    "smtplib",
    "sqlite3",
    "subprocess",
    "asyncio",
    "sched",
    "threading",
    "multiprocessing",
    "shelve",
}


def find_forbidden_imports(source: str) -> list[str]:
    """Return a description of every import in ``source`` the domain may not use."""
    violations: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:  # relative import: stays inside the package
                continue
            modules = [node.module or ""]
        else:
            continue
        for module in modules:
            top = module.split(".")[0]
            if top == "app":
                if module != "app.domain" and not module.startswith("app.domain."):
                    violations.append(module)
            elif top not in STDLIB or top in FORBIDDEN_STDLIB_IO:
                violations.append(module)
    return violations


def domain_files() -> list[Path]:
    return sorted(DOMAIN_DIR.rglob("*.py"))


def test_the_scan_actually_finds_domain_modules() -> None:
    names = {p.name for p in domain_files()}
    # Guards against this test passing vacuously after a move or rename.
    assert {"order_state_machine.py", "exceptions.py", "order.py", "audit.py"} <= names


@pytest.mark.parametrize("path", domain_files(), ids=lambda p: str(p.relative_to(DOMAIN_DIR)))
def test_domain_module_imports_only_stdlib_and_domain(path: Path) -> None:
    assert find_forbidden_imports(path.read_text()) == []


class TestTheDetectorItself:
    @pytest.mark.parametrize(
        "source",
        [
            "import sqlalchemy",
            "from sqlalchemy.ext.asyncio import AsyncSession",
            "import redis.asyncio as redis",
            "from fastapi import FastAPI",
            "import httpx",
            "import aiomqtt",
            "import paho.mqtt.client",
            "from pydantic import BaseModel",
            "from app.db.session import get_session",
            "from app.api.internal import router",
            "import app.dispatch.assignment",
            "import socket",
            "import asyncio",
            "from urllib.request import urlopen",
            "def f():\n    import sqlalchemy",
        ],
    )
    def test_flags_infrastructure_imports(self, source: str) -> None:
        assert find_forbidden_imports(source) != []

    @pytest.mark.parametrize(
        "source",
        [
            "from __future__ import annotations",
            "import math\nfrom dataclasses import dataclass\nfrom uuid import UUID",
            "from app.domain.exceptions import DomainError",
            "import app.domain.models",
            "from .order import Order",
            "from typing import TYPE_CHECKING",
        ],
    )
    def test_allows_stdlib_and_domain_imports(self, source: str) -> None:
        assert find_forbidden_imports(source) == []
