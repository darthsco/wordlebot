"""Entry point: `python -m wordlebot [--config PATH] [command]`.

Commands:
  run (default)   start the bot
  sync            refresh the solution cache and exit
  pick            print the starter that would be chosen for the next target date (dry run)
  admin ACTION    manage admin panel accounts:
                    list | add NAME | passwd NAME | reset-2fa NAME | remove NAME
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
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
    if not db.admin_names():
        logging.getLogger("wordlebot").warning(
            "The admin panel is enabled but has no admins; add one with `python -m wordlebot admin add NAME`"
        )
    return await panel.start(panel.Panel(service, auth, bot), config.web.host, config.web.port)


def _ask_password() -> str:
    from .auth import password_problem

    while True:
        password = getpass.getpass("Password: ")
        if problem := password_problem(password):
            print(problem, file=sys.stderr)
            continue
        if getpass.getpass("Repeat: ") != password:
            print("The passwords don't match.", file=sys.stderr)
            continue
        return password


def _admin(config: Config, action: str, username: str | None) -> int:
    """Manage panel accounts from the host; the only way to create the first admin
    or recover one who lost their authenticator."""
    from .auth import hash_password, valid_username

    db = Database(config.storage.database_path, config.schedule)
    try:
        if action == "list":
            for name in db.admin_names():
                admin = db.admin(name)
                print(f"{name}{'' if admin and admin.totp_enabled else '  (two-factor not set up yet)'}")
            return 0
        if not username:
            print(f"admin {action} needs a username", file=sys.stderr)
            return 2
        if action == "add":
            if not valid_username(username):
                print("Usernames are 1-64 printable characters without surrounding spaces.", file=sys.stderr)
                return 2
            if db.admin(username):
                print(f"{username} already exists.", file=sys.stderr)
                return 1
            db.add_admin(username, hash_password(_ask_password()))
            db.audit("cli", "admin_added", username)
            print(f"Added {username}. They set up two-factor sign-in at their first login.")
            return 0
        if db.admin(username) is None:
            print(f"No admin named {username}.", file=sys.stderr)
            return 1
        if action == "passwd":
            db.set_admin_password(username, hash_password(_ask_password()))
            db.delete_sessions(username)
            db.audit("cli", "password_reset", username)
            print(f"Password changed; {username}'s sessions were ended.")
        elif action == "reset-2fa":
            db.set_admin_totp(username, None, enabled=False)
            db.delete_sessions(username)
            db.audit("cli", "totp_reset", username)
            print(f"Two-factor reset; {username} sets it up again at their next login.")
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
    parser.add_argument("action", nargs="?", choices=["list", "add", "passwd", "reset-2fa", "remove"])
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
            parser.error("admin needs an action: list, add, passwd, reset-2fa or remove")
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
