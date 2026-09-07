"""Reading and writing the git-ignored .env file."""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV = Path(".env")


def load_env(path: Path = DEFAULT_ENV) -> None:
    """Load KEY=VALUE lines into the environment. Real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), _unquote(value.strip()))


def read_env(path: Path = DEFAULT_ENV) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = _unquote(value.strip())
    return values


def set_env_value(key: str, value: str, path: Path = DEFAULT_ENV) -> None:
    """Set KEY=value in .env, updating the line in place if the key is already there."""
    lines = path.read_text().splitlines() if path.exists() else []
    replaced = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.split("=", 1)[0].strip() == key:
            lines[i] = f"{key}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n")
    os.environ[key] = value


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def mask(value: str) -> str:
    """Show enough of an id to recognise it without printing the whole thing."""
    return value if len(value) <= 8 else f"{value[:8]}...{value[-4:]}"
