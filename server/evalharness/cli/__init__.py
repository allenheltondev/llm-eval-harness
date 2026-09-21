"""The ``evalharness`` command-line interface.

:mod:`evalharness.cli.main` holds the parser and the ``main`` entry point the
console script points at (see ``[project.scripts]``), with the command bodies
in :mod:`evalharness.cli.commands` and the terminal rendering in
:mod:`evalharness.cli.render`.

Nothing is re-exported here on purpose: ``from evalharness.cli import main``
returning the *function* would shadow the module of the same name, so
``evalharness.cli.main`` would mean one thing to an importer and another to a
reader.
"""
