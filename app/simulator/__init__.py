"""A drone simulator: plays the drone side of the MQTT protocol, for development and tests.

It is **not** part of the platform. It receives the same five commands a real drone would,
checks the mission checksum and the Rule 2 start authorisation like a real drone should,
flies the predefined mission on a (fast-forwardable) clock, and reports progress events.
"""
