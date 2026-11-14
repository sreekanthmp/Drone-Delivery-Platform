"""Live state and live notifications: a *view* of the platform, never its source of truth.

Everything here is best-effort and rebuildable. The latest position of a drone, the active
alerts and the stream of changes live in Redis; if Redis disappears, dispatch, commands and
the audit trail keep working and only live tracking degrades. Nothing in this package holds
or decides an order, flight or drone status (Rule 4): statuses live in PostgreSQL and are
written only through the services.
"""
