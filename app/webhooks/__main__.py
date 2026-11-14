"""``python -m app.webhooks``: run the webhook sender (wired in ``app.webhook_worker``)."""

from app.webhook_worker import main

raise SystemExit(main())
