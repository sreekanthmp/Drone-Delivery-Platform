"""Client adapters (Rule 3).

External partners never talk to core business logic. Each integration has its own
adapter package that:

* receives the partner's payload and validates it,
* maps it into the internal :class:`~app.domain.models.OrderRequest`,
* renders responses and errors in the partner's format,
* translates internal status updates into the partner's vocabulary.

Adapters depend only on ``app.domain`` and Pydantic (enforced by
``tests/unit/test_import_boundaries.py``), and core code never imports them.

NOTE: the Swiggy and Zomato payload formats here are *illustrative*, not the
partners' real specifications. Replace the mapping inside the respective package
when the real contracts are available; nothing else needs to change.
"""
