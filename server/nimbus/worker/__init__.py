"""The cloud evaluation lane's worker host.

This package is the *host* for cloud-lane evaluations, not the evaluation logic
itself. It is deployed as a Python 3.12 Lambda (see ``docs/cloud-evals-infra.md``)
which the FastAPI server invokes asynchronously, and it does exactly three
things:

1. :mod:`~nimbus.worker.lambda_app` — the handler. Validates the event,
   writes the job's opening state, and runs the evaluation to a terminal state
   inside the invocation, stopping cleanly if it nears Lambda's time limit.
2. :mod:`~nimbus.worker.ddb` — persists everything the evaluation produces
   to the shared DynamoDB table using the item shapes in ``docs/cloud-evals.md``.
3. :mod:`~nimbus.worker.interfaces` — the narrow seam between this host and
   ``nimbus.evals``, so the two can be built and changed independently.

Nothing in here imports FastAPI, SQLModel, or any other server-only machinery:
the artifact ships the evaluation engine and its dependencies, not the web app.
"""

from __future__ import annotations

__all__ = ["lambda_app", "ddb", "interfaces"]
