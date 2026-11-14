"""Telemetry: what drones report back.

Inbound only. Nothing in this package can publish to a drone or touch the database: it
decodes events and hands them to the dispatch service, which decides whether and how they
change anything (Rules 1 and 4).
"""
