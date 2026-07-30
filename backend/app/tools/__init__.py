"""Operator tools, runnable as ``python -m app.tools.<name>``.

Each is a thin CLI over logic that lives in the application package, so the same
checks run from the command line, at startup and behind the health endpoint, and
cannot drift apart. Every tool:

* prints a human-readable report by default and machine-readable JSON with
  ``--json``, so it is equally usable by a person and by CI;
* exits non-zero on failure, so a pipeline step can depend on the result rather
  than grepping output;
* never mutates anything. Tools report; ``cip`` applies.
"""
