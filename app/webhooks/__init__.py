"""Webhooks: telling partners, in their own format, that an order's status changed.

The queue is the ``webhook_deliveries`` table, written in the same transaction as the order's
audit event (so a status change cannot exist without its notification). The sender here is a
separate process: it claims due deliveries, asks the partner's adapter to render each one
(Rule 3), POSTs it, and records what happened. It reads orders and never changes one (Rule 4),
and it knows nothing about drones (Rule 1).
"""
