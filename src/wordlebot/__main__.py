"""Entry point: `python -m wordlebot [--config PATH] [command]`.

Commands:
  run (default)   start the bot
  sync            refresh the solution cache and exit
  pick            print the starter that would be chosen for the next target date (dry run)
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import aiohttp
from dotenv import load_dotenv

from .config import load_config
from .db import Database
from .service import WordleService
from .solutions import NytSolutionSource
from .words import WordLists


async def _offline(command: str, config_path: str) -> None:
    config = load_config(config_path)
    db = Database(config.storage.database_path)
    lists = WordLists.load(config.storage.words_dir, config.game.word_length)
    async with aiohttp.ClientSession() as session:
        service = WordleService(config, db, NytSolutionSource(session, config.solutions.base_url), lists)
        await service.refresh()
        if command == "pick":
            starter = await service.choose_starter(service.target_date())
            print(f"#{starter.puzzle_number} {starter.target_date}: {starter.word.upper()}")
        else:
            print(f"{len(db.solution_dates())} solutions cached")
    db.close()


async def _run(config_path: str) -> None:
    from .bot import WordleBot

    config = load_config(config_path)
    if not config.token:
        raise SystemExit("DISCORD_TOKEN is not set (see .env.example)")
    db = Database(config.storage.database_path)
    lists = WordLists.load(config.storage.words_dir, config.game.word_length)
    async with aiohttp.ClientSession() as session:
        service = WordleService(config, db, NytSolutionSource(session, config.solutions.base_url), lists)
        bot = WordleBot(config, service)
        async with bot:
            await bot.start(config.token)


def main() -> None:
    parser = argparse.ArgumentParser(prog="wordlebot")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "sync", "pick"])
    args = parser.parse_args()

    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.command == "run":
        asyncio.run(_run(args.config))
    else:
        asyncio.run(_offline(args.command, args.config))


if __name__ == "__main__":
    main()
