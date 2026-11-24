"""Agent jobs: finished coding-agent jobs reported by Updraft, compared by model.

Nimbus executes its own evaluations; this package is the other direction. An
Updraft Cloud deployment publishes a ``Job Finished`` event to EventBridge for
each job its coding agents finish (software-factory-cli #110). A rule in this
stack delivers each one to :mod:`nimbus.agentjobs.lambda_app`, which stores it
as an ``AGENTJOB#`` item, and ``GET /api/v1/agent-jobs/compare`` groups the
stored jobs by model.

The data is *observational*: real jobs, not a controlled benchmark. See
``docs/agent-jobs.md`` for what that does and does not let a comparison claim.
"""
