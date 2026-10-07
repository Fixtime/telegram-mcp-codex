"""Interactive owner-only setup. Run in your terminal, never inside a Codex chat."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import os
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.sessions import StringSession

from .config import ConfigError, Settings, default_directory, peer_id, private_write


async def initialize(directory: Path, scope):
    if not sys.stdin.isatty():
        raise ConfigError("Setup requires your own interactive terminal.")
    if directory.exists():
        raise ConfigError(
            "Directory already exists; choose a new one to avoid overwriting a session."
        )
    api_id = int(input("Telegram API ID (my.telegram.org/apps): "))
    api_hash = getpass.getpass("Telegram API hash (hidden): ").strip()
    phone = getpass.getpass("Phone number including country code (hidden): ").strip()
    # Validate configuration before opening any network connection.
    config = {
        "version": 1,
        "api_id": api_id,
        "api_hash": api_hash,
        "account_id": 1,
        "chat_scope": scope,
        "timezone": "Europe/Moscow",
    }
    path = directory / "config.json"
    directory.mkdir(mode=0o700, parents=True)
    try:
        private_write(path, json.dumps(config))
        Settings.load(path)
        client = TelegramClient(
            StringSession(),
            api_id,
            api_hash,
            receive_updates=False,
            flood_sleep_threshold=0,
            request_retries=1,
            device_model="Codex Telegram Analysis",
            app_version="1.0.0",
        )
        try:
            await client.connect()
            sent = await client.send_code_request(phone)
            code = getpass.getpass("Telegram login code (hidden): ").strip()
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
            except SessionPasswordNeededError:
                await client.sign_in(password=getpass.getpass("Telegram 2FA password (hidden): "))
            me = await client.get_me()
            if me is None or me.bot:
                raise ConfigError("A personal Telegram account is required.")
            config["account_id"] = me.id
            # Write a complete temporary config and replace only our own provisional file.
            temp = directory / "config.ready"
            private_write(temp, json.dumps(config, indent=2) + "\n")
            private_write(directory / "session.secret", client.session.save() + "\n")
            temp.replace(path)
        finally:
            await client.disconnect()
    except BaseException:
        # Preserve a completed session if a late disconnect fails; never silently lose it.
        if not (directory / "session.secret").exists():
            path.unlink(missing_ok=True)
            (directory / "config.ready").unlink(missing_ok=True)
            directory.rmdir()
        raise
    print("Setup complete. Session saved privately; nothing secret was printed.")
    print(
        "Access: all account chats/channels/groups."
        if scope == "all"
        else "Access: selected canonical IDs."
    )
    print("Restart the configured MCP server in Codex to activate it.")


def main():
    parser = argparse.ArgumentParser(
        description="Create a private Telegram session outside Codex."
    )
    parser.add_argument("--directory", type=Path, default=default_directory())
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--all-chats", action="store_true", help="Explicitly allow reads of the whole account."
    )
    scope.add_argument("--chat-ids", help="Comma-separated canonical numeric IDs; no usernames.")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    os.umask(0o077)
    try:
        selected = (
            "all"
            if args.all_chats
            else [peer_id(int(v.strip())) for v in args.chat_ids.split(",")]
        )
        asyncio.run(initialize(args.directory.absolute(), selected))
    except (Exception, KeyboardInterrupt):
        print(
            "Setup did not complete. Check credentials/connectivity in your terminal; "
            "no credentials or provider errors are printed.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
