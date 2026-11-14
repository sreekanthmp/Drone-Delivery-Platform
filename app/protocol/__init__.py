"""The MQTT wire contract between the platform and its drones.

A neutral layer: it knows the domain and nothing else, so the drone link (which sends
commands), the telemetry ingest (which reads events) and the drone simulator (which plays
the drone) can all share one definition without depending on each other.
"""
