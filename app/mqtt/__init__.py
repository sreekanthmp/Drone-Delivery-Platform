"""MQTT infrastructure: a reconnecting client around aiomqtt.

Knows nothing about drones or the domain: it moves bytes between topics and handlers.
"""
