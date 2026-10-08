#!/usr/bin/env python3
"""Fail when commit candidates contain common credential patterns."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", "local_private", "__pycache__"}
TEXT_SUFFIXES = {
    ".py", ".ipynb", ".md", ".txt", ".json", ".yaml", ".yml",
    ".toml", ".sh", ".env", ".csv", ".ini", ".cfg",
}
PATTERNS = {
    "OpenAI key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "AWS access key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "Private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "Literal credential": re.compile(
        r"(?i)(?:api[_-]?key|api_openai|aws_secret_access_key|"
        r"mysql_password|password|secret|token)\s*[:=]\s*['\"][^'\"]{12,}['\"]"
    ),
}


def staged_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return [ROOT / line for line in result.stdout.splitlines() if line.strip()]


def working_paths() -> list[Path]:
    return [
        path for path in ROOT.rglob("*")
        if path.is_file()
        and not SKIP_PARTS.intersection(path.relative_to(ROOT).parts)
    ]


def candidate_paths() -> list[Path]:
    staged = staged_paths()
    return staged if staged else working_paths()


def main() -> int:
    findings: list[str] = []
    for path in candidate_paths():
        relative = path.relative_to(ROOT)
        if SKIP_PARTS.intersection(relative.parts):
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in {
            "Dockerfile", ".env", ".env.example",
        }:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for label, pattern in PATTERNS.items():
            for match in pattern.finditer(text):
                line = text.count("\n", 0, match.start()) + 1
                findings.append(f"{relative}:{line}: {label}")

    if findings:
        print("Se detectaron posibles credenciales:", file=sys.stderr)
        print("\n".join(f"- {finding}" for finding in findings), file=sys.stderr)
        return 1
    print("OK: no se detectaron credenciales en los archivos candidatos")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
