"""The ``nimbus`` command-line interface.

:mod:`nimbus.cli.main` holds the parser and the ``main`` entry point the
console script points at (see ``[project.scripts]``), with the command bodies
in :mod:`nimbus.cli.commands` and the terminal rendering in
:mod:`nimbus.cli.render`.

Nothing is re-exported here on purpose: ``from nimbus.cli import main``
returning the *function* would shadow the module of the same name, so
``nimbus.cli.main`` would mean one thing to an importer and another to a
reader.
"""
