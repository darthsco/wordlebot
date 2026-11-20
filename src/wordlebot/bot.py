"""Discord layer: the posting loop, score intake, and slash commands.

There is no timer aimed at the post time. A short-interval loop asks the service
what the channel is owed and sends it, so a missed or failed post is made up on a
later tick and nothing is posted twice.

The bot registers a single slash command, `/wordlebot`, with each operation a
subcommand (`/wordlebot results puzzle:12`), so it shares the command picker with
other bots without name collisions. Its output is delivered by DM (with an ephemeral fallback if the user's
DMs are closed) so leaderboards don't clutter the channel. Every command defers
its interaction before doing anything else: Discord drops an interaction that is
not acknowledged within 3 seconds, while a deferred one can be answered with
followups for 15 minutes.
"""

from __future__ import annotations

import asyncio
import functools
import io
import logging
from collections.abc import Awaitable, Callable, Collection
from dataclasses import asdict
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import tasks

from .config import Config
from .scores import MISMATCH, NO_STARTER, PENDING, VERIFIED
from .db import Starter
from .service import Actor, Attachment, GolfMessage, PuzzleInPlay, SubmissionsExist, WordleService
from .version import version_info

log = logging.getLogger(__name__)

DISCORD_LIMIT = 2000
STATUS_REACTIONS = {VERIFIED: "✅", PENDING: "⏳", MISMATCH: "❌", NO_STARTER: "➖"}
DUPLICATE_REACTION = "🔁"
RECONCILE_SECONDS = 60  # also the retry delay after a failed post
# How long a report command waits for a missing solution before answering with
# what is cached. Commands defer first, so this is about the user's patience, not
# Discord's 3-second acknowledgement deadline.
REPORT_REFRESH_SECONDS = 10
COMMAND_FAILED = "Something went wrong; see the bot's log."
COMMAND = "wordlebot"  # the one top-level slash command; operations are subcommands
REROLL_IN_PLAY = (
    "Not rerolling: with `target_offset_days = 0` the starter is for today's puzzle, "
    "which is already live, so members may have opened with it. "
    "A starter can only be rerolled before its puzzle starts."
)

REROLL_HAS_SUBMISSIONS = (
    "Not rerolling: {count} submission(s) already exist for Wordle {puzzle:,}, "
    "and changing the starter now would invalidate them."
)


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


def _file(image: Attachment) -> discord.File:
    """A fresh `discord.File` for `image`: one can be sent only once."""
    return discord.File(io.BytesIO(image.data), filename=image.filename)


class WordleBot(discord.Client):
    def __init__(self, config: Config, service: WordleService) -> None:
        intents = discord.Intents.default()
        intents.message_content = True  # privileged: enable in the Developer Portal
        # Nothing the bot sends is meant to ping anyone, and its posts echo
        # member-chosen display names. A message that should ping must pass its
        # own allowed_mentions at the call site.
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        assert config is service.config
        self.service = service
        self.started_at = datetime.now(timezone.utc)
        self.tree = app_commands.CommandTree(self)
        # Serializes everything that picks or announces a starter (the loop, reroll).
        self._post_lock = asyncio.Lock()
        self._background: set[asyncio.Task[None]] = set()  # keeps tasks referenced
        self.reconcile = tasks.loop(seconds=RECONCILE_SECONDS)(self._reconcile)
        self.reconcile.before_loop(self.wait_until_ready)
        self._register_commands()

    @property
    def config(self) -> Config:
        """The service's config, which the admin panel can replace while running."""
        return self.service.config

    # --- lifecycle -------------------------------------------------------

    async def setup_hook(self) -> None:
        guild = discord.Object(id=self.config.discord.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        self.reconcile.start()

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)
        try:
            async with self._post_lock:
                await self.service.refresh()
        except Exception:
            log.exception("Solution refresh failed; continuing with cached data")
        await self._update_reactions()

    async def channel(self, channel_id: int | None = None) -> discord.abc.Messageable:
        """The channel with `channel_id`; by default the configured one."""
        channel_id = channel_id or self.config.discord.channel_id
        channel = self.get_channel(channel_id)
        if channel is None:
            channel = await self.fetch_channel(channel_id)
        return channel  # type: ignore[return-value]

    async def _send(
        self, text: str, *, ping: Collection[int] = (), image: Attachment | None = None
    ) -> int:
        """Post `text` to the channel, split to fit; returns the last message's id.

        Only the users in `ping` are notified by their mentions. `image` is
        attached to the last message.
        """
        channel = await self.channel()
        kwargs = {}
        if ping:
            users = [discord.Object(id=u) for u in ping]
            kwargs["allowed_mentions"] = discord.AllowedMentions(
                everyone=False, roles=False, users=users
            )
        parts = chunks(text)
        for i, part in enumerate(parts):
            if image is not None and i == len(parts) - 1:
                kwargs["file"] = _file(image)
            message = await channel.send(part, **kwargs)
        return message.id

    async def _edit(self, message_id: int, text: str, *, image: Attachment | None = None) -> None:
        """Replace the text of one of the bot's messages in the channel, and its
        attachments with `image` if given."""
        channel = await self.channel()
        kwargs = {"attachments": [_file(image)]} if image is not None else {}
        await channel.get_partial_message(message_id).edit(content=text, **kwargs)  # type: ignore[attr-defined]

    # --- posting loop ----------------------------------------------------

    async def _reconcile(self) -> None:
        # An unhandled exception would stop the tasks.loop for good, so log it and
        # let the next tick retry.
        try:
            await self._tick()
        except Exception:
            log.exception("Posting failed; will retry")

    async def _tick(self) -> None:
        try:
            # Picks up today's solution shortly after local midnight, so shares
            # pasted before the evening post can be verified.
            await self.service.refresh_if_missing()
            async with self._post_lock:
                await self.service.post_due(self._send)
        finally:
            await self._update_reactions()

    # --- reactions -------------------------------------------------------

    async def _update_reactions(self) -> None:
        """Swap the status reaction on every share whose status changed since last
        time: ⏳ for the outcome once settled, or whatever an admin changed."""
        for update in self.service.take_reaction_updates():
            score = update.score
            if score.channel_id is None or score.message_id is None:
                continue  # stored before submissions recorded their message, or by an admin
            try:
                channel = await self.channel(score.channel_id)
                message = channel.get_partial_message(score.message_id)  # type: ignore[attr-defined]
            except Exception:
                log.warning("Could not find message %s to update its reaction", score.message_id)
                continue
            # Separate attempts: the new reaction is worth adding even if the old
            # one can't be removed. A deleted message or missing permission fails both.
            if update.previous is not None:
                try:
                    await message.remove_reaction(STATUS_REACTIONS[update.previous], self.user)
                except discord.HTTPException:
                    log.warning("Could not remove the %s reaction on %s", update.previous, score.message_id)
            if update.current is not None:
                try:
                    await message.add_reaction(STATUS_REACTIONS[update.current])
                except discord.HTTPException:
                    log.warning("Could not react to message %s", score.message_id)

    # --- admin panel hooks -----------------------------------------------
    #
    # The panel (web.py) calls these, so it takes the same lock and updates the
    # same reactions as the loop and the slash commands.

    async def panel_send(self, text: str, *, image: Attachment | None = None) -> int:
        return await self._send(text, image=image)

    async def panel_edit(self, message_id: int, text: str, *, image: Attachment | None = None) -> None:
        await self._edit(message_id, text, image=image)

    async def panel_reroll(self, actor: Actor) -> Starter:
        async with self._post_lock:
            return await self.service.reroll(self._send, actor)

    async def panel_tick(self) -> None:
        await self._tick()

    async def panel_settle(self, actor: Actor) -> int:
        try:
            async with self._post_lock:
                return await self.service.settle_pending(actor)
        finally:
            await self._update_reactions()

    async def panel_update_reactions(self) -> None:
        await self._update_reactions()

    def panel_gateway(self) -> tuple[bool, str | None, float | None]:
        """(connected, bot user, heartbeat latency in ms)."""
        ready = self.is_ready() and not self.is_closed()
        latency = self.latency * 1000 if ready and self.latency == self.latency else None  # NaN check
        return ready, str(self.user) if self.user else None, latency

    async def _refresh_for_report(self) -> None:
        """Give a missing solution a moment to arrive before a report is built.

        The refresh carries on in the background if it is slow, and the report is
        built from what is cached.
        """

        async def refresh() -> None:
            await self.service.refresh_if_missing()
            await self._update_reactions()

        task = asyncio.create_task(refresh())
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        try:
            await asyncio.wait_for(asyncio.shield(task), REPORT_REFRESH_SECONDS)
        except asyncio.TimeoutError:
            log.info("Solution refresh still running; reporting from cached data")
        except Exception:
            log.exception("Solution refresh failed; reporting from cached data")

    # --- score intake ----------------------------------------------------

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.channel.id != self.config.discord.channel_id:
            return
        submission = await self.service.submit(
            message.author.id,
            message.author.display_name,
            message.content,
            message.channel.id,
            message.id,
        )
        if submission is not None:
            emoji = (
                STATUS_REACTIONS[submission.status] if submission.inserted else DUPLICATE_REACTION
            )
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                log.warning("Could not react to message %s", message.id)
        # Submitting may have fetched a solution that settles earlier shares too.
        await self._update_reactions()

    # --- slash commands --------------------------------------------------

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        if not isinstance(member, discord.Member):
            return False
        if member.guild_permissions.manage_guild:
            return True
        return any(r.id in self.config.discord.admin_role_ids for r in member.roles)

    async def _dm(self, interaction: discord.Interaction, text: str) -> None:
        """Send `text` to the invoking user by DM; fall back to ephemeral followups.

        The interaction must already be deferred (see `_deferred`).
        """
        await self._dm_messages(interaction, [GolfMessage(part) for part in chunks(text)])

    async def _dm_messages(
        self, interaction: discord.Interaction, messages: list[GolfMessage]
    ) -> None:
        """`_dm` for messages already split to fit, each with its image if any."""
        def files(m: GolfMessage) -> dict[str, discord.File]:
            return {"file": _file(m.image)} if m.image is not None else {}

        try:
            for m in messages:
                await interaction.user.send(m.text, **files(m))
        except discord.Forbidden:
            for m in messages:
                await interaction.followup.send(m.text, ephemeral=True, **files(m))
            return
        await interaction.followup.send("Sent you a DM. 📬", ephemeral=True)

    def _deferred(
        self, callback: Callable[..., Awaitable[None]]
    ) -> Callable[..., Awaitable[None]]:
        """Wrap a slash command: acknowledge at once, and report a failure.

        The body must answer through `interaction.followup` only; a second
        `interaction.response` call would raise InteractionResponded.
        """

        @functools.wraps(callback)  # discord.py reads the options off the signature
        async def wrapper(interaction: discord.Interaction, *args: object, **kwargs: object) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                await callback(interaction, *args, **kwargs)
            except Exception:
                # Without a followup the user would watch "thinking…" until it expires.
                log.exception("Command /%s %s failed", COMMAND, callback.__name__)
                try:
                    await interaction.followup.send(COMMAND_FAILED, ephemeral=True)
                except discord.HTTPException:
                    log.warning(
                        "Could not report the failure of /%s %s", COMMAND, callback.__name__
                    )

        return wrapper

    def _register_commands(self) -> None:
        service = self.service
        group = app_commands.Group(
            name=COMMAND, description="Wordle starters, results and standings"
        )

        @group.command(description="Show today's and the upcoming starting word")
        @self._deferred
        async def starter(interaction: discord.Interaction) -> None:
            lines = []
            today = service.today()  # one reading, so both lines agree on the date
            if current := service.current_starter(today):
                lines.append(f"Today (#{current.puzzle_number:,}): `{current.word.upper()}`")
            if upcoming := service.upcoming_starter(today):
                lines.append(f"Next (#{upcoming.puzzle_number:,}): `{upcoming.word.upper()}`")
            await self._dm(interaction, "\n".join(lines) or "No starter posted yet.")

        @group.command(
            description="Results for one puzzle (default: the one the daily post reports on)"
        )
        @app_commands.describe(
            puzzle="Wordle puzzle number (default: the puzzle before the next starter's)"
        )
        @self._deferred
        async def results(interaction: discord.Interaction, puzzle: int | None = None) -> None:
            await self._refresh_for_report()
            await self._dm(interaction, service.daily_results(puzzle))

        @group.command(description="Standings for a period")
        @app_commands.choices(
            period=[
                app_commands.Choice(name="weekly", value="weekly"),
                app_commands.Choice(name="monthly", value="monthly"),
                app_commands.Choice(name="all-time", value="all_time"),
            ]
        )
        @self._deferred
        async def leaderboard(
            interaction: discord.Interaction, period: app_commands.Choice[str]
        ) -> None:
            report = {
                "weekly": service.weekly,
                "monthly": service.monthly,
                "all_time": service.all_time,
            }[period.value]
            await self._refresh_for_report()
            await self._dm(interaction, report())

        @group.command(description="The golf round in progress: par 4 a puzzle, 18 to a round")
        @self._deferred
        async def golf(interaction: discord.Interaction) -> None:
            await self._refresh_for_report()
            await self._dm_messages(interaction, service.golf_standings())

        @group.command(description="Our recent starting words")
        @app_commands.describe(count="How many to show (default 10)")
        @self._deferred
        async def history(
            interaction: discord.Interaction, count: app_commands.Range[int, 1, 100] = 10
        ) -> None:
            await self._dm(interaction, service.history(count))

        @group.command(description="Show the bot's configuration")
        @self._deferred
        async def config(interaction: discord.Interaction) -> None:
            cfg = asdict(self.config)
            cfg.pop("token", None)
            body = "\n".join(
                f"[{section}]\n" + "\n".join(f"{k} = {v!r}" for k, v in values.items())
                for section, values in cfg.items()
            )
            await self._dm(interaction, f"```toml\n{body}\n```")

        @group.command(description="Show which version of the bot is running")
        @self._deferred
        async def version(interaction: discord.Interaction) -> None:
            await self._dm(interaction, version_info())

        @group.command(description="(Admin) Pick and post a new upcoming starting word")
        @self._deferred
        async def reroll(interaction: discord.Interaction) -> None:
            if not self._is_admin(interaction):
                await interaction.followup.send("Admins only.", ephemeral=True)
                return
            try:
                async with self._post_lock:
                    starter = await service.reroll(self._send)
            except PuzzleInPlay:
                await interaction.followup.send(REROLL_IN_PLAY, ephemeral=True)
                return
            except SubmissionsExist as refused:
                await interaction.followup.send(
                    REROLL_HAS_SUBMISSIONS.format(
                        count=refused.count, puzzle=refused.puzzle_number
                    ),
                    ephemeral=True,
                )
                return
            except Exception:
                # A pick that was saved but not sent is announced by the posting loop.
                log.exception("Reroll failed")
                await interaction.followup.send("Reroll failed; see the log.", ephemeral=True)
                return
            await interaction.followup.send(f"Rerolled to `{starter.word.upper()}`.", ephemeral=True)

        self.tree.add_command(group)
