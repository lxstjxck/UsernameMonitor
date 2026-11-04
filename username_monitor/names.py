"""Username list validation. Final validity is always decided by Telegram."""
from pathlib import Path
import re

# Telegram's method documents length 5-32, ASCII letters, digits and underscore.
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_]{5,32}$")


def load_names(path: Path) -> tuple[list[str], list[str]]:
    if not path.exists():
        raise FileNotFoundError(f"Нет списка имён: {path}. Создайте usernames.txt")
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.removeprefix("@").lower()
        if not USERNAME_PATTERN.fullmatch(name):
            invalid.append(line)
        elif name not in seen:
            valid.append(name)
            seen.add(name)
    return valid, invalid
