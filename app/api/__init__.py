"""HTTP API (FastAPI).

Endpoints stay thin: they translate HTTP to service calls and back. Business rules
live in ``app.domain`` and ``app.services``; partner formats live in ``app.adapters``.
The API layer never touches the database layer directly.
"""
