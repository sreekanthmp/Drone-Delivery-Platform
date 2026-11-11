"""Application services.

Services orchestrate the pure domain (state machine) with persistence. They are
the only place that turns a ``TransitionResult`` into stored data (Rule 4).
"""
