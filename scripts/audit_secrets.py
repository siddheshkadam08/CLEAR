#!/usr/bin/env python3
"""Fail the build if a real credential is committed.

A one-off grep finds today's leak; it does nothing about the next one. This runs
over every git-tracked file, so it covers source, examples, documentation, compose
files and deploy scripts alike - the leak this was written after was a database
password inside a comment in `migrations/env.py`, quoting an error message, which
no amount of watching `.env*` would have caught.

Two rules, deliberately different in kind:

* **Shape rules** match credential formats that are unmistakably real - a live
  API key has a recognisable prefix and length, and there is no legitimate reason
  for one to appear in a repository.
* **Assignment rules** match `SOMETHING_PASSWORD=value` where the value is not a
  placeholder. This is the one that catches ordinary mistakes, so "what counts as
  a placeholder" is defined generously: empty, `<angle-bracketed>`, `change-me`,
  and the well-known local-development defaults are all fine.

Exit code 1 on a finding, so it can gate CI.

Usage:
    python scripts/audit_secrets.py
    python scripts/audit_secrets.py --verbose
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: Formats that are recognisably live credentials. Prefix plus length, so a
#: placeholder like `sk-ant-xxx` does not trip them.
SHAPE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("Anthropic API key", re.compile(r"sk-ant-[A-Za-z0-9_-]{24,}")),
    ("OpenAI API key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("OpenRouter API key", re.compile(r"sk-or-v1-[A-Za-z0-9]{32,}")),
    ("AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("Slack token", re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
]

#: Credential-bearing settings. Matched as assignments so prose is not flagged.
ASSIGNMENT_RULE = re.compile(
    r"(?P<key>[A-Z0-9_]*(?:PASSWORD|SECRET|API_KEY|TOKEN|ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*)"
    r"\s*[=:]\s*"
    r"(?P<quote>['\"]?)(?P<value>[^\s'\"#,}]{1,200})(?P=quote)"
)

#: A DSN with an inline password: postgresql://user:secret@host
DSN_RULE = re.compile(r"(?P<scheme>[a-z+]{3,})://(?P<user>[^:/\s]+):(?P<password>[^@/\s]+)@")

#: Values that are obviously not real. Compared case-insensitively.
PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "",
        "-",
        "none",
        "null",
        "changeme",
        "change-me",
        "change_me",
        "changethis",
        "placeholder",
        "your-key-here",
        "your_api_key",
        "xxx",
        "todo",
        "example",
        "test",
        "dummy",
        "fake",
        "redacted",
        "secret",
        "password",
        # Well-known local-only defaults for the bundled dev containers. These are
        # not credentials to anything reachable from outside a developer's machine.
        "minioadmin",
        "cip_dev_password",
        "postgres",
        "redis",
        "guest",
        # The documented first-run admin password. Not a shipped credential: the
        # production config validator refuses to start while SEED_ADMIN_PASSWORD or
        # NEW_USER_DEFAULT_PASSWORD still holds it (see Settings.production_problems).
        # It is a "change-me" that happens to look like a password.
        "abc@1234",
    }
)

#: Substrings that mark a value as a template rather than a credential.
PLACEHOLDER_MARKERS: tuple[str, ...] = (
    "<",
    ">",
    "${",
    "{{",
    "change-me",
    "change_me",
    "changeme",
    "your-",
    "your_",
    "xxxxx",
    "example.com",
    "-here",
    "replace",
    # A truncated example in documentation, e.g. `NVIDIA_API_KEY=nvapi-...`.
    "...",
    # Fixtures for CI and tests. Prefixed by convention precisely so they are
    # identifiable as non-credentials; the convention is only worth having if
    # something enforces it, which is this list.
    "ci-",
    "test-",
    "fake-",
    "dummy-",
    "rotated-",
)

#: Settings whose value is a name, not a credential. ``IDOC_API_KEY_HEADER`` holds
#: the *header* an API key travels in - matching it is a pure false positive, and
#: false positives are what get an audit tool switched off.
NON_SECRET_SUFFIXES: tuple[str, ...] = (
    "_HEADER",
    "_HEADER_NAME",
    "_ENV",
    "_FILE",
    "_PATH",
    "_URL",
    "_ENABLED",
    "_SCHEME",
    "_ALGORITHM",
    "_TTL",
    "_EXPIRY",
    "_EXPIRED",
    "_INVALID",
    "_REQUIRED",
    "_MISSING",
    "_TOKENS",
    "_LENGTH",
)

#: A value that is the key restated - an enum member or a constant, not a secret.
#: ``BELOW_MIN_TOKENS = "below_min_tokens"`` is the shape.
def _is_key_restated(key: str, value: str) -> bool:
    normalised = value.strip().lower().replace("-", "_").replace(".", "_")
    return normalised == key.lower()


#: Code, not a literal: ``os.environ.get(...)``, ``z.string()``, ``process.env.X``.
CODE_MARKERS: tuple[str, ...] = (
    "(",
    ")",
    "os.",
    "z.",
    "process.env",
    "self.",
    "settings.",
    "config.",
    "getenv",
)

#: Paths that legitimately contain credential-shaped test data.
SKIP_PATHS: tuple[str, ...] = (
    "scripts/audit_secrets.py",
    "package-lock.json",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
)

SKIP_SUFFIXES: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".pdf",
    ".ico",
    ".woff",
    ".woff2",
    ".zip",
    ".docx",
    ".xlsx",
)


@dataclass(slots=True)
class Finding:
    path: str
    line: int
    rule: str
    excerpt: str

    def render(self) -> str:
        return f"  {self.path}:{self.line}\n      [{self.rule}] {self.excerpt}"


def _is_placeholder(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in PLACEHOLDERS:
        return True
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return True
    # An env-var reference is a reference, not a value.
    if lowered.startswith("$"):
        return True
    # Too short to be a credential worth protecting.
    return len(lowered) < 8


def _redact(value: str) -> str:
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * (len(value) - 6)}{value[-3:]}"


def tracked_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    )
    paths = []
    for line in result.stdout.splitlines():
        if not line or line in SKIP_PATHS or line.endswith(SKIP_SUFFIXES):
            continue
        paths.append(REPO / line)
    return paths


def scan_file(path: Path) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []

    relative = path.relative_to(REPO).as_posix()
    findings: list[Finding] = []

    for number, line in enumerate(text.splitlines(), start=1):
        if len(line) > 2000:  # minified asset
            continue

        for name, pattern in SHAPE_RULES:
            match = pattern.search(line)
            if match:
                findings.append(Finding(relative, number, name, _redact(match.group(0))))

        for match in ASSIGNMENT_RULE.finditer(line):
            key, value = match.group("key"), match.group("value")
            if key.endswith(NON_SECRET_SUFFIXES):
                continue
            if _is_key_restated(key, value):
                continue
            if any(marker in value for marker in CODE_MARKERS):
                continue
            # `${VAR:-default}` reached here as a bare `-default` because the regex
            # stops at `}`. The default inside a compose interpolation is a local
            # fallback, not a committed credential.
            if "${" in line:
                continue
            if not _is_placeholder(value):
                findings.append(
                    Finding(
                        relative,
                        number,
                        f"{key} has a non-placeholder value",
                        f"{key}={_redact(value)}",
                    )
                )

        for match in DSN_RULE.finditer(line):
            password = match.group("password")
            if not _is_placeholder(password):
                findings.append(
                    Finding(
                        relative,
                        number,
                        "connection string with an inline password",
                        f"{match.group('scheme')}://{match.group('user')}:"
                        f"{_redact(password)}@...",
                    )
                )

    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true", help="Report files scanned.")
    args = parser.parse_args()

    files = tracked_files()
    findings: list[Finding] = []
    for path in files:
        findings.extend(scan_file(path))

    if args.verbose:
        print(f"Scanned {len(files)} tracked files.")

    if not findings:
        print(f"No credentials found in {len(files)} tracked files.")
        return 0

    print(f"\n{len(findings)} possible credential(s) in tracked files:\n", file=sys.stderr)
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    print(
        "\nMove real values into `.env` (git-ignored) and leave a placeholder here.\n"
        "If a finding is a false positive, use a value this script recognises as a\n"
        "placeholder - see PLACEHOLDERS and PLACEHOLDER_MARKERS.\n"
        "\nA credential that was ever committed is compromised: rotate it, do not\n"
        "only delete it. Removing it from the working tree leaves it in the history.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
