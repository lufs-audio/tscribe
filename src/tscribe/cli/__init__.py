"""tscribe.cli — the unified CLI surface (Unit 07).

Public surface is deliberately tiny: `main`, so `pyproject.toml`'s
`[project.scripts] tscribe = "tscribe.cli:main"` entry point keeps working
unchanged even though this used to be a flat `cli.py` module and is now a
package. See `main.py` for argument parsing/dispatch, `commands.py` for the
one-function-per-subcommand glue, and `output.py` for the --json envelope
and exit-code contract.
"""

from .main import main
from .output import CliResponse, ExitCode

__all__ = ["main", "ExitCode", "CliResponse"]
