"""The pre-rename handler path, kept for one release of the eval worker.

The worker Lambda's ``Handler`` named ``evalharness.worker.lambda_app.handler``
before the rename to Nimbus. CloudFormation updates a function's configuration
and its code in separate calls, and invocations run in between, so changing the
handler in the same deploy that changes the code leaves a window where the
configured handler names a module the deployed code does not have. Keeping the
old name for one release -- served by this shim, which the worker zip carries
next to ``nimbus`` -- means the handler never changes in the deploy that renames
the code. The next release points ``Handler`` at ``nimbus`` and drops this.

Not part of the ``nimbus`` package: scripts/package-eval-worker.sh copies it
into the worker zip, and nothing else ships it.
"""
