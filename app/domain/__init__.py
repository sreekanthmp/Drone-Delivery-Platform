"""Pure business logic.

Nothing under ``app.domain`` may perform I/O or import infrastructure
(database, Redis, MQTT, HTTP, web frameworks). This is enforced by
``tests/unit/test_domain_purity.py``.
"""
