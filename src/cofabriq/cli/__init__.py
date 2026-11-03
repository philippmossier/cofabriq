"""Command-line entry point (`cofabriq ...`). Importing the command modules
registers their commands on `app`: `--help` lists the context layer's first,
then the issue commands, then the loop's (`eval` registers on its own group)."""

from cofabriq.cli import context, evals, issues, loop
from cofabriq.cli.common import app

__all__ = ["app", "context", "evals", "issues", "loop"]
