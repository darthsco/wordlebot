"""Entry point: `python -m wordlebot [--config PATH] [command]`.

Commands:
  run (default)   start the bot
  sync            refresh the solution cache and exit
  pick            print the starter that would be chosen for the next target date (dry run)
  admin ACTION    manage admin panel accounts:
                    list | add NAME | enroll NAME | reset-passkeys NAME | remove NAME
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from typing import TYPE_CHECKING

import aiohttp
from dotenv import load_dotenv

from .config import Config, load_config
from .db import Database
from .service import WordleService
from .solutions import NytSolutionSource
from .words import WordLists

if TYPE_CHECKING:
    from aiohttp import web

    from .bot import WordleBot


async def _offline(command: str, config: Config) -> None:
    db = Database(config.storage.database_path, config.schedule)
    lists = WordLists.load(config.storage.words_dir, config.game.word_length)
    async with aiohttp.ClientSession() as session:
        service = WordleService(config, db, NytSolutionSource(session, config.solutions.base_url), lists)
        service.load_overrides()  # so `pick` chooses the way the bot would
        await service.refresh()
        if command == "pick":
            starter = await service.choose_starter(service.target_date())
            print(f"#{starter.puzzle_number} {starter.target_date}: {starter.word.upper()}")
        else:
            print(f"{len(db.solution_dates())} solutions cached")
    db.close()


async def _run(config: Config) -> None:
    from .bot import WordleBot

    if not config.token:
        raise SystemExit("DISCORD_TOKEN is not set (see .env.example)")
    db = Database(config.storage.database_path, config.schedule)
    lists = WordLists.load(config.storage.words_dir, config.game.word_length)
    async with aiohttp.ClientSession() as session:
        service = WordleService(config, db, NytSolutionSource(session, config.solutions.base_url), lists)
        service.load_overrides()  # settings edited in the admin panel
        bot = WordleBot(service.config, service)
        runner = await _start_panel(config, db, service, bot)
        try:
            async with bot:
                await bot.start(config.token)
        finally:
            if runner is not None:
                await runner.cleanup()


async def _start_panel(
    config: Config, db: Database, service: WordleService, bot: WordleBot
) -> web.AppRunner | None:
    if not config.web.enabled:
        return None
    from . import web as panel
    from .auth import Auth

    auth = Auth(db, config.web)
    auth.prune_sessions()
    log = logging.getLogger("wordlebot")
    if not db.admin_names():
        log.warning(
            "The admin panel is enabled but has no admins; add one with `python -m wordlebot admin add NAME`"
        )
    for name in db.admin_names():
        if not db.passkeys(name):
            log.warning(
                "Admin %s has no passkey and can't sign in; make them a link with "
                "`python -m wordlebot admin enroll %s`", name, name,
            )
    return await panel.start(panel.Panel(service, auth, bot), config.web.host, config.web.port)


ADMIN_ACTIONS = ["list", "add", "enroll", "reset-passkeys", "remove"]


def _print_enrollment(config: Config, db: Database, username: str) -> None:
    from .auth import ENROLLMENT_LIFETIME, Auth

    token = Auth(db, config.web).issue_enrollment(username)
    hours = int(ENROLLMENT_LIFETIME.total_seconds() // 3600)
    if config.web.domain:
        link = f"https://{config.web.domain}/enroll/{token}"
    else:
        link = f"/enroll/{token}  (after the panel's https:// address; [web] domain is not set)"
    print(f"Enrollment link for {username} (works once, for {hours} hours; replaces any earlier link):")
    print(f"  {link}")
    print("Send it privately: whoever opens it first registers a passkey for this account.")


def _admin(config: Config, action: str, username: str | None) -> int:
    """Manage panel accounts from the host; the only way to create an admin or let
    one who lost every passkey back in."""
    from .auth import valid_username

    db = Database(config.storage.database_path, config.schedule)
    try:
        if action == "list":
            for name in db.admin_names():
                count = len(db.passkeys(name))
                print(f"{name}  ({count} passkey{'' if count == 1 else 's'})" if count else f"{name}  (no passkey yet)")
            return 0
        if not username:
            print(f"admin {action} needs a username", file=sys.stderr)
            return 2
        if action == "add":
            if not valid_username(username):
                print("Usernames are 1-64 printable characters without surrounding spaces.", file=sys.stderr)
                return 2
            if not db.add_admin(username):
                print(f"{username} already exists.", file=sys.stderr)
                return 1
            db.audit("cli", "admin_added", username)
            print(f"Added {username}.")
            _print_enrollment(config, db, username)
            return 0
        if db.admin(username) is None:
            print(f"No admin named {username}.", file=sys.stderr)
            return 1
        if action == "enroll":
            db.audit("cli", "enrollment_link", username)
            _print_enrollment(config, db, username)
        elif action == "reset-passkeys":
            removed = db.remove_passkeys(username)
            db.delete_sessions(username)
            db.audit("cli", "passkeys_reset", f"{username}: {removed} removed")
            print(f"Removed {removed} passkey(s) and ended {username}'s sessions.")
            _print_enrollment(config, db, username)
        elif action == "remove":
            db.remove_admin(username)
            db.audit("cli", "admin_removed", username)
            print(f"Removed {username}.")
        return 0
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="wordlebot")
    parser.add_argument("--config", default=os.environ.get("WORDLEBOT_CONFIG", "config.toml"))
    parser.add_argument("command", nargs="?", default="run", choices=["run", "sync", "pick", "admin"])
    parser.add_argument("action", nargs="?", choices=ADMIN_ACTIONS)
    parser.add_argument("username", nargs="?")
    load_dotenv()
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Only `run` needs the config file and Discord IDs; sync/pick work from defaults.
    try:
        config = load_config(args.config, for_run=args.command == "run")
    except ValueError as exc:  # includes tomllib.TOMLDecodeError
        raise SystemExit(f"{args.config}: {exc}") from None
    if args.command == "admin":
        if args.action is None:
            parser.error(f"admin needs an action: {', '.join(ADMIN_ACTIONS)}")
        raise SystemExit(_admin(config, args.action, args.username))
    if args.action is not None:
        parser.error(f"unexpected argument {args.action!r}")
    try:
        if args.command == "run":
            asyncio.run(_run(config))
        else:
            asyncio.run(_offline(args.command, config))
    except KeyboardInterrupt:
        # Ctrl-C, or `docker stop` (the image's stop signal is SIGINT). By now
        # asyncio.run has cancelled the main task and `async with bot:` has
        # closed the gateway session, so this is a clean stop: exit 0, no traceback.
        logging.getLogger("wordlebot").info("Shutting down")


if __name__ == "__main__":
    main()
