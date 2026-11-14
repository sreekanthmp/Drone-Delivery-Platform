"""Live WebSocket streams (read-only).

A stream sends what is happening; it never accepts anything to act on. Whatever a client
sends is ignored (Rule 1: there is no way to steer or command a drone from here), and the
only service calls are reads (an architecture test lists them).
"""
