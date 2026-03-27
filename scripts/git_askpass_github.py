#!/usr/bin/env python3
"""GIT_ASKPASS: отдаёт логин/пароль для https://github.com из .env (GITHUB_TOKEN)."""
import re
import sys
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def read_token() -> str:
    env_path = repo_root() / ".env"
    if not env_path.is_file():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^GITHUB_TOKEN=(.*)$", line.strip())
        if m:
            return m.group(1).strip().strip('"').strip("'")
    return ""


def main() -> None:
    prompt = " ".join(sys.argv[1:]).lower()
    if "username" in prompt:
        print("x-access-token")
        return
    if "password" in prompt or "passphrase" in prompt:
        t = read_token()
        if not t:
            sys.exit(1)
        print(t)
        return
    print("")


if __name__ == "__main__":
    main()
