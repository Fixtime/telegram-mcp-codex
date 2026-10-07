"""Explicit, fail-closed policy and private credential storage."""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigError(ValueError):
    pass


def default_directory() -> Path:
    return Path.home() / ".local" / "share" / "telegram-analysis"


def private_read(path: Path) -> str:
    """Refuse symlinks, permissive modes, non-regular and oversized files."""
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o077:
        raise ConfigError("Credential directory must be a real directory with mode 0700.")
    if hasattr(os, "getuid") and parent.st_uid != os.getuid():
        raise ConfigError("Credential directory must belong to the service user.")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 32768:
            raise ConfigError("Credential files must be regular, mode 0600, and at most 32 KiB.")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise ConfigError("Credential file must belong to the service user.")
        return stream.read()


def private_write(path: Path, text: str) -> None:
    """Create once without overwriting existing credentials."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)


def integer(value, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{name} must be an integer between {low} and {high}.")
    return value


def peer_id(value) -> int:
    # Canonical Telethon IDs: user >0, basic group -id, channel -10^12-id.
    if type(value) is not int or value == 0 or abs(value) >= 2**63:
        raise ConfigError("chat_id must be a nonzero canonical integer ID.")
    return value


@dataclass(frozen=True)
class Settings:
    api_id: int
    api_hash: str
    account_id: int
    chat_scope: str | frozenset[int]
    directory: Path
    timezone: str = "Europe/Moscow"
    max_days: int = 31
    max_messages: int = 200
    max_dialogs: int = 5000
    max_text_chars: int = 8192
    max_output_bytes: int = 196608
    calls_per_minute: int = 30
    timeout_seconds: int = 35

    @classmethod
    def load(cls, config: Path) -> "Settings":
        try:
            data = json.loads(private_read(config))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError("Cannot read private configuration. Run local setup first.") from exc
        if not isinstance(data, dict):
            raise ConfigError("Configuration must be a JSON object.")
        fields = {
            "version",
            "api_id",
            "api_hash",
            "account_id",
            "chat_scope",
            "timezone",
            "max_days",
            "max_messages",
            "max_dialogs",
            "max_text_chars",
            "max_output_bytes",
            "calls_per_minute",
            "timeout_seconds",
        }
        if set(data) - fields or data.get("version") != 1:
            raise ConfigError("Unsupported configuration version or unknown setting.")
        api_id = integer(data.get("api_id"), "api_id", 1, 2**31 - 1)
        account_id = integer(data.get("account_id"), "account_id", 1, 2**63 - 1)
        api_hash = data.get("api_hash")
        if not isinstance(api_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
            raise ConfigError("Invalid api_hash.")
        scope = data.get("chat_scope")
        if scope != "all":
            if not isinstance(scope, list) or not scope:
                raise ConfigError("chat_scope must be explicitly 'all' or a nonempty list of IDs.")
            scope = frozenset(peer_id(item) for item in scope)
        tz = data.get("timezone", "Europe/Moscow")
        try:
            ZoneInfo(tz)
        except (ValueError, TypeError, KeyError) as exc:
            raise ConfigError("Invalid timezone.") from exc
        bounds = {
            "max_days": (31, 1, 366),
            "max_messages": (200, 1, 500),
            "max_dialogs": (5000, 1, 10000),
            "max_text_chars": (8192, 256, 32768),
            "max_output_bytes": (196608, 4096, 262144),
            "calls_per_minute": (30, 1, 120),
            "timeout_seconds": (35, 1, 55),
        }
        limits = {
            name: integer(data.get(name, default), name, low, high)
            for name, (default, low, high) in bounds.items()
        }
        if limits["max_output_bytes"] < 4 * limits["max_text_chars"] + 4096:
            raise ConfigError(
                "max_output_bytes must fit one maximum Unicode message plus metadata."
            )
        return cls(api_id, api_hash, account_id, scope, config.parent, timezone=tz, **limits)

    def permits(self, chat: int) -> bool:
        chat = peer_id(chat)
        return self.chat_scope == "all" or chat in self.chat_scope

    def session(self) -> str:
        value = private_read(self.directory / "session.secret").strip()
        if not value:
            raise ConfigError("Session is missing. Run local setup.")
        return value
