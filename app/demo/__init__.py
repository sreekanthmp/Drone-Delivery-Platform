"""A one-command demo of the real platform: ``python -m app.demo``.

Everything runs in one process with no Docker, database, broker or Redis: the platform's own
dispatch, permission gate, state machines, audit trail, drone link and live streams, with the
database in memory and two simulated drones on the other end of the link. It serves a single
web page with a map. It is for looking at the idea, not for running a business.
"""
