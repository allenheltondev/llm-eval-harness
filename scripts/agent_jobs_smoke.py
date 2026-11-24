#!/usr/bin/env python3
"""Smoke-test agent-job ingestion on a deployed stack (docs/agent-jobs.md, #68).

Sends one synthetic Updraft ``Job Finished`` event to the bus, waits for the
stack to store it, checks the ingest dead-letter queue, and deletes the item.
The logic is :mod:`nimbus.agentjobs.smoke`; this resolves the stack's outputs
and reports.

Needs AWS credentials that may PutEvents on the bus and GetItem / DeleteItem on
the stack's table, and a stack deployed with the eval worker.

Usage::

    make smoke-agent-jobs
    # or
    cd server && uv run python ../scripts/agent_jobs_smoke.py \\
        --stack llm-eval-harness

Exit code is 0 only if every check passes.
"""

from __future__ import annotations

import argparse
import sys

import boto3

from nimbus.agentjobs import smoke


def _outputs(cloudformation, stack: str) -> dict[str, str]:
    described = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
    return {o["OutputKey"]: o["OutputValue"] for o in described.get("Outputs", [])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent_jobs_smoke", description=__doc__.splitlines()[0])
    parser.add_argument("--stack", default="llm-eval-harness", help="this harness's stack name")
    parser.add_argument(
        "--bus", default="default", help="the bus Updraft publishes to (default: the default bus)"
    )
    parser.add_argument("--region", default=None)
    parser.add_argument("--timeout", type=float, default=smoke.DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)

    session = boto3.session.Session(region_name=args.region)
    outputs = _outputs(session.client("cloudformation"), args.stack)
    missing = [k for k in ("TableName", "AgentJobIngestDeadLetterQueueUrl") if k not in outputs]
    if missing:
        print(
            f"agent_jobs_smoke: stack {args.stack!r} has no {', '.join(missing)} output -- "
            "is it deployed with the eval worker?",
            file=sys.stderr,
        )
        return 2

    result = smoke.run(
        events=session.client("events"),
        table=session.resource("dynamodb").Table(outputs["TableName"]),
        sqs=session.client("sqs"),
        bus=args.bus,
        queue_url=outputs["AgentJobIngestDeadLetterQueueUrl"],
        timeout=args.timeout,
    )
    for name, passed, detail in result.checks:
        print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
