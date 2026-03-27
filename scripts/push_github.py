#!/usr/bin/env python3
"""
Безопасный push в origin: токен только через GIT_ASKPASS, вывод чистится от секретов.
Запуск: python3 scripts/push_github.py [ветка]  (по умолчанию main)
"""
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASKPASS = Path(__file__).resolve().parent / "git_askpass_github.py"


def redact_output(text: str) -> str:
    if not text:
        return text
    text = re.sub(
        r"x-access-token:[^@\s]+@",
        "x-access-token:***@",
        text,
        flags=re.I,
    )
    text = re.sub(r"ghp_[A-Za-z0-9]{20,}", "ghp_REDACTED", text, flags=re.I)
    text = re.sub(r"github_pat_[A-Za-z0-9_]+", "github_pat_REDACTED", text, flags=re.I)
    return text


def main() -> None:
    branch = sys.argv[1] if len(sys.argv) > 1 else "main"
    env = os.environ.copy()
    env["GIT_ASKPASS"] = str(ASKPASS)
    env["GIT_TERMINAL_PROMPT"] = "0"

    r = subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(redact_output(r.stdout))
    sys.stderr.write(redact_output(r.stderr))
    raise SystemExit(r.returncode)


if __name__ == "__main__":
    main()
