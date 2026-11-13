"""Discord layer: daily scheduled post, score intake, and slash commands.

Slash command output is delivered by DM (with an ephemeral fallback if the user's
DMs are closed) so leaderboards don't clutter the channel.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

import discord
from discord import app_commands
from discord.ext import tasks

from .config import Config
from .scores import MISMATCH, NO_STARTER, PENDING, VERIFIED
from .service import WordleService

log = logging.getLogger(__name__)

DISCORD_LIMIT = 2000
STATUS_REACTIONS = {VERIFIED: "✅", PENDING: "⏳", MISMATCH: "❌", NO_STARTER: "➖"}
DUPLICATE_REACTION = "🔁"


def chunks(text: str, limit: int = DISCORD_LIMIT) -> list[str]:
    out, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            out.append(current)
            current = ""
        current += line[:limit]
    if current:
        out.append(current)
    return out


def starter_announcement(word: str, puzzle_number: int, target: object) -> str:
    return (
        f"🟩 **Starting word for Wordle {puzzle_number:,}** ({target:%A, %B %d}):\n"
        f"# `{word.upper()}`\n"
        "Open with this word, then paste your share result here to be counted."
    )


class WordleBot(discord.Client):
    def __init__(self, config: Config, service: WordleService) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: enable in the Developer Portal
        super().__init__(intents=intents)
        self.config = config
        self.service = service
        self.tree = app_commands.CommandTree(self)
        self.daily_post = tasks.loop(time=config.schedule.post_time_local)(self._daily_post)
        self._register_commands()

    # --- lifecycle -------------------------------------------------------

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.discord.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.daily_post.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)
        await self.service.refresh()
        if self.service.post_is_due():
            log.info("Missed today's post; posting now")
            await self._daily_post()

    async def channel(self) -> discord.abc.Messageable:
        channel = self.get_channel(self.config.discord.channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.config.discord.channel_id)
        return channel  # type: ignore[return-value]

    # --- daily post ------------------------------------------------------

    async def _daily_post(self) -> None:
        # An unhandled exception would stop the tasks.loop for good, so log and move on.
        try:
            await self._post_daily()
        except Exception:
            log.exception("Daily post failed")

    async def _post_daily(self) -> None:
        try:
            await self.service.refresh()
        except Exception:
            log.exception("Solution refresh failed; continuing with cached data")

        channel = await self.channel()
        if self.config.schedule.post_daily_results:
            for part in chunks(self.service.daily_results()):
                await channel.send(part)

        target = self.service.target_date()
        if existing := self.service.db.starter_for_date(target):
            # Already picked earlier (e.g. /reroll), so remind instead of picking again.
            await channel.send(
                "⏰ **REMINDER** "
                + starter_announcement(existing.word, existing.puzzle_number, target)
            )
            log.info("Starter for %s already posted; sent reminder", target)
            return
        starter = await self.service.choose_starter(target)
        message = await channel.send(
            starter_announcement(starter.word, starter.puzzle_number, starter.target_date)
        )
        self.service.db.save_starter(starter, message.id)
        log.info("Posted starter %s for %s", starter.word, target)

    # --- score intake ----------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.channel.id != self.config.discord.channel_id:
            return
        submission = self.service.submit(
            message.author.id, message.author.display_name, message.content
        )
        if submission is None:
            return
        emoji = STATUS_REACTIONS[submission.status] if submission.inserted else DUPLICATE_REACTION
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            log.warning("Could not react to message %s", message.id)

    # --- slash commands --------------------------------------------------

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.manage_guild:
            return True
        return any(r.id in self.config.discord.admin_role_ids for r in member.roles)

    async def _dm(self, interaction: discord.Interaction, text: str) -> None:
        """Send `text` to the invoking user by DM; fall back to an ephemeral reply."""
        try:
            for part in chunks(text):
                await interaction.user.send(part)
            await interaction.response.send_message("Sent you a DM. 📬", ephemeral=True)
        except discord.Forbidden:
            parts = chunks(text)
            await interaction.response.send_message(parts[0], ephemeral=True)
            for part in parts[1:]:
                await interaction.followup.send(part, ephemeral=True)

    def _register_commands(self) -> None:
        tree, service = self.tree, self.service

        @tree.command(description="Show today's and the upcoming starting word")
        async def starter(interaction: discord.Interaction) -> None:
            lines = []
            if current := service.current_starter():
                lines.append(f"Today (#{current.puzzle_number:,}): `{current.word.upper()}`")
            if upcoming := service.upcoming_starter():
                lines.append(f"Next (#{upcoming.puzzle_number:,}): `{upcoming.word.upper()}`")
            await self._dm(interaction, "\n".join(lines) or "No starter posted yet.")

        @tree.command(description="Results for one puzzle (defaults to today's)")
        @app_commands.describe(puzzle="Wordle puzzle number")
        async def results(interaction: discord.Interaction, puzzle: int | None = None) -> None:
            await self._dm(interaction, service.daily_results(puzzle))

        @tree.command(description="Standings for a period")
        @app_commands.choices(
            period=[
                app_commands.Choice(name="weekly", value="weekly"),
                app_commands.Choice(name="monthly", value="monthly"),
                app_commands.Choice(name="all-time", value="all_time"),
            ]
        )
        async def leaderboard(
            interaction: discord.Interaction, period: app_commands.Choice[str]
        ) -> None:
            report = {
                "weekly": service.weekly,
                "monthly": service.monthly,
                "all_time": service.all_time,
            }[period.value]
            await self._dm(interaction, report())

        @tree.command(description="Our recent starting words")
        @app_commands.describe(count="How many to show (default 10)")
        async def history(
            interaction: discord.Interaction, count: app_commands.Range[int, 1, 100] = 10
        ) -> None:
            await self._dm(interaction, service.history(count))

        @tree.command(description="Show the bot's configuration")
        async def config(interaction: discord.Interaction) -> None:
            cfg = asdict(self.config)
            cfg.pop("token", None)
            body = "\n".join(
                f"[{section}]\n" + "\n".join(f"{k} = {v!r}" for k, v in values.items())
                for section, values in cfg.items()
            )
            await self._dm(interaction, f"```toml\n{body}\n```")

        @tree.command(description="(Admin) Pick and post a new upcoming starting word")
        async def reroll(interaction: discord.Interaction) -> None:
            if not self._is_admin(interaction):
                await interaction.response.send_message("Admins only.", ephemeral=True)
                return
            target = service.target_date()
            previous = service.db.starter_for_date(target)
            if target <= service.today():
                await interaction.response.send_message(
                    "That puzzle is already in play; not rerolling.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            exclude = {previous.word} if previous else set()
            starter = await service.choose_starter(target, exclude)
            channel = await self.channel()
            message = await channel.send(
                "🔁 **Rerolled!** "
                + starter_announcement(starter.word, starter.puzzle_number, target)
            )
            service.db.save_starter(starter, message.id)
            await interaction.followup.send(f"Rerolled to `{starter.word.upper()}`.", ephemeral=True)
