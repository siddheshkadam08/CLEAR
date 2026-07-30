"""Shared plumbing for the operator tools.

Exit codes are part of each tool's contract - a CI step gates on them - so they are
defined once here rather than invented per tool.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Coroutine
from typing import Any

#: Exit codes. 0 success, 1 a real failure, 2 a warning the operator asked to treat
#: as a failure, 3 the tool could not run at all (bad arguments, no configuration).
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_WARNING = 2
EXIT_UNUSABLE = 3

_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_DIM = "\033[2m"
_RESET = "\033[0m"


def _colour_enabled() -> bool:
    return sys.stdout.isatty()


def mark(ok: bool | None) -> str:
    """``ok`` / ``FAIL`` / ``warn`` with colour when attached to a terminal."""
    if ok is None:
        label, colour = "warn", _YELLOW
    elif ok:
        label, colour = " ok ", _GREEN
    else:
        label, colour = "FAIL", _RED
    return f"[{colour}{label}{_RESET}]" if _colour_enabled() else f"[{label}]"


def dim(text: str) -> str:
    return f"{_DIM}{text}{_RESET}" if _colour_enabled() else text


def heading(text: str) -> str:
    return f"\n{text}\n{'-' * len(text)}"


def emit(payload: dict[str, Any], human: Callable[[], None], *, as_json: bool) -> None:
    """Render one report, either as JSON on stdout or as text."""
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        human()


def base_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Emit machine-readable JSON instead of a human report.",
    )
    return parser


def configure_tool_logging(as_json: bool) -> None:
    """Quieten the application's own logging for tool output.

    In JSON mode this is not cosmetic: stdout has to be *only* the report, or
    `... --json | jq` fails on the first structlog line. Silenced entirely rather
    than redirected, because a tool that prints its findings has no use for a
    parallel log stream saying the same things.
    """
    import logging

    from app.core.logging import configure_logging

    level = "CRITICAL" if as_json else "WARNING"
    configure_logging(level=level)
    # `configure_logging` is idempotent, so if anything configured logging during
    # import the call above is a no-op. Setting the stdlib level directly is what
    # actually takes effect, and structlog's stdlib integration honours it.
    logging.getLogger().setLevel(getattr(logging, level))
    for name in ("app", "httpx", "sqlalchemy", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(getattr(logging, level))


def run(
    main: Callable[[argparse.Namespace], Coroutine[Any, Any, int]],
    args: argparse.Namespace,
) -> int:
    """Run an async tool body, translating an interrupt into a clean exit."""
    try:
        return asyncio.run(main(args))
    except KeyboardInterrupt:  # pragma: no cover - operator ctrl-c
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_UNUSABLE


__all__ = [
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_UNUSABLE",
    "EXIT_WARNING",
    "base_parser",
    "configure_tool_logging",
    "dim",
    "emit",
    "heading",
    "mark",
    "run",
]
