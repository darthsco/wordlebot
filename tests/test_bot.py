import asyncio
import logging
from types import SimpleNamespace

import discord
from test_service import (
    BAD_SHARE,
    GOOD_SHARE,
    RESULTS,
    SOLUTION,
    TODAY,
    TOMORROW,
    TOMORROW_SHARE,
    Channel,
    make,
    word_in,
)
from test_solutions import FakeSource

from wordlebot.bot import COMMAND_FAILED, REROLL_IN_PLAY, WordleBot
from wordlebot.service import REROLL_PREFIX


class Message:
    def __init__(self, id):
        self.id = id


class Share:
    """A member's message in the channel, recording the bot's reactions to it."""

    def __init__(self, channel, id, user_id, content):
        self.id, self.content, self.channel = id, content, channel
        self.author = SimpleNamespace(bot=False, id=user_id, display_name=f"user{user_id}")
        self.reactions = []
        self.deleted = False
        channel.shares[id] = self

    def _check(self):
        if self.deleted:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")

    async def add_reaction(self, emoji):
        self._check()
        self.reactions.append(emoji)

    async def remove_reaction(self, emoji, member):
        self._check()
        self.reactions.remove(emoji)


class DiscordChannel(Channel):
    id = 0  # Config's default channel_id

    def __init__(self):
        super().__init__()
        self.shares = {}

    async def send(self, text):
        return Message(await super().send(text))

    def get_partial_message(self, message_id):
        return self.shares[message_id]


def make_bot(tmp_path, **kwargs):
    service, clock, _ = make(tmp_path, **kwargs)
    bot = WordleBot(service.config, service)
    channel = DiscordChannel()

    async def get_channel(channel_id=None):
        return channel

    bot.channel = get_channel
    return bot, clock, channel


def test_overlapping_ticks_post_one_starter(tmp_path):
    bot, _, channel = make_bot(tmp_path)

    async def run():
        await asyncio.gather(bot._tick(), bot._tick(), bot._tick())

    asyncio.run(run())
    assert len(channel.starters()) == 1
    assert len([t for t in channel.sent if RESULTS in t]) == 1
    assert bot.service.db.starter_for_date(TOMORROW).word == word_in(channel.starters()[0])


def test_failed_tick_is_logged_and_the_next_one_retries(tmp_path, caplog):
    bot, _, channel = make_bot(tmp_path, post_daily_results=False)
    channel.fail = 1
    with caplog.at_level(logging.ERROR):
        asyncio.run(bot._reconcile())  # must not raise, or tasks.loop would stop
    assert "Posting failed" in caplog.text
    picked = bot.service.db.starter_for_date(TOMORROW)

    asyncio.run(bot._reconcile())
    assert [word_in(t) for t in channel.sent] == [picked.word]
    asyncio.run(bot._reconcile())
    assert len(channel.sent) == 1


class Interaction:
    """Enforces Discord's rules: one initial response, followups only after it."""

    def __init__(self, user=None):
        self.replies = []
        self.events = []  # "defer", "dm" and "followup", in order
        self.responded = False
        self.response = SimpleNamespace(defer=self._defer, send_message=self._send_message)
        self.followup = SimpleNamespace(send=self._followup)
        self.user = user or User(self)

    def _respond(self):
        if self.responded:
            raise discord.InteractionResponded(self)
        self.responded = True

    async def _defer(self, ephemeral=False, thinking=False):
        self._respond()
        assert ephemeral, "a public defer would make the followups public too"
        self.events.append("defer")

    async def _send_message(self, text, ephemeral=False):
        self._respond()
        self.replies.append(text)

    async def _followup(self, text, ephemeral=False):
        assert self.responded, "a followup needs an initial response"
        assert ephemeral
        self.events.append("followup")
        self.replies.append(text)


class User:
    def __init__(self, interaction, dms_open=True):
        self.interaction, self.dms_open = interaction, dms_open
        self.dms = []

    async def send(self, text):
        if not self.dms_open:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "closed")
        self.interaction.events.append("dm")
        self.dms.append(text)


def reroll(bot):
    interaction = Interaction()
    asyncio.run(bot.tree.get_command("reroll").callback(interaction))
    return interaction.replies


def test_reroll_command_announces_and_marks_the_new_word(tmp_path):
    bot, _, channel = make_bot(tmp_path)
    assert reroll(bot) == ["Admins only."]

    bot._is_admin = lambda interaction: True
    asyncio.run(bot._tick())
    before = bot.service.db.starter_for_date(TOMORROW).word
    replies = reroll(bot)

    after = bot.service.db.starter_for_date(TOMORROW)
    assert after.word != before and after.announced
    assert channel.sent[-1].startswith(REROLL_PREFIX) and word_in(channel.sent[-1]) == after.word
    assert replies == [f"Rerolled to `{after.word.upper()}`."]
    asyncio.run(bot._tick())
    assert len(channel.starters()) == 2  # no extra post after the reroll


def test_reroll_command_refuses_a_puzzle_in_play(tmp_path):
    bot, _, channel = make_bot(tmp_path, hour=15, target_offset_days=0)
    bot._is_admin = lambda interaction: True
    assert reroll(bot) == [REROLL_IN_PLAY]
    assert "already live" in REROLL_IN_PLAY and "target_offset_days = 0" in REROLL_IN_PLAY
    assert channel.sent == []


def test_reroll_command_refuses_once_shares_exist_for_the_puzzle(tmp_path):
    bot, _, channel = make_bot(tmp_path)
    bot._is_admin = lambda interaction: True
    asyncio.run(bot._tick())
    before = bot.service.db.starter_for_date(TOMORROW)
    sent = len(channel.sent)
    paste(bot, channel, 501, 1, TOMORROW_SHARE)  # played in a timezone ahead of ours

    (reply,) = reroll(bot)
    assert "Not rerolling" in reply and "1 submission(s)" in reply and "Wordle 13" in reply
    assert bot.service.db.starter_for_date(TOMORROW) == before
    assert len(channel.sent) == sent


def test_results_command_defaults_to_the_puzzle_the_daily_post_reports(tmp_path):
    bot, _, _ = make_bot(tmp_path, hour=7, target_offset_days=0, post_time="07:00")
    assert "Wordle 11" in command(bot, "results", None)
    assert "Wordle 12" in command(bot, "results", 12)
    description = bot.tree.get_command("results").description
    assert "daily post" in description and len(description) <= 100


# --- score intake and reactions ----------------------------------------------


def make_morning_bot(tmp_path, words):
    bot, clock, channel = make_bot(tmp_path, hour=8)
    bot.service.source = FakeSource(words)
    return bot, clock, channel


def paste(bot, channel, id, user_id, content):
    share = Share(channel, id, user_id, content)
    asyncio.run(bot.on_message(share))
    return share


def test_morning_shares_get_their_verdict_straight_away(tmp_path):
    bot, _, channel = make_morning_bot(tmp_path, {TODAY: SOLUTION})
    assert paste(bot, channel, 501, 1, GOOD_SHARE).reactions == ["✅"]
    assert paste(bot, channel, 502, 2, BAD_SHARE).reactions == ["❌"]
    assert paste(bot, channel, 503, 1, GOOD_SHARE).reactions == ["🔁"]


def test_tick_caches_todays_solution_when_no_post_is_owed(tmp_path):
    bot, _, channel = make_morning_bot(tmp_path, {TODAY: SOLUTION})
    asyncio.run(bot._tick())
    assert TODAY in bot.service.db.solution_dates()
    assert channel.sent == []


def test_pending_reaction_is_swapped_when_the_score_is_settled(tmp_path):
    bot, clock, channel = make_morning_bot(tmp_path, {})  # the NYT is down
    alice = paste(bot, channel, 501, 1, GOOD_SHARE)
    bob = paste(bot, channel, 502, 2, BAD_SHARE)
    carol = paste(bot, channel, 503, 3, GOOD_SHARE)
    assert alice.reactions == bob.reactions == carol.reactions == ["⏳"]
    carol.deleted = True

    bot.service.source.words[TODAY] = SOLUTION
    clock.set(TODAY, 8, 30)
    asyncio.run(bot._reconcile())  # a deleted message must not stop the others
    assert alice.reactions == ["✅"]
    assert bob.reactions == ["❌"]
    assert carol.reactions == ["⏳"]
    asyncio.run(bot._reconcile())
    assert alice.reactions == ["✅"]  # updated once


def test_share_pasted_before_its_starter_gets_a_verdict_later(tmp_path):
    bot, clock, channel = make_morning_bot(tmp_path, {TODAY: SOLUTION, TOMORROW: "audio"})
    early = paste(bot, channel, 501, 1, TOMORROW_SHARE)  # hours before the evening post
    assert early.reactions == ["⏳"]

    clock.set(TODAY, 20)
    asyncio.run(bot._tick())
    assert len(channel.starters()) == 1 and early.reactions == ["⏳"]

    opened_with_it = bot.service.db.starter_for_date(TOMORROW).word == "fuzzy"
    clock.set(TOMORROW, 0, 1)
    asyncio.run(bot._tick())
    assert early.reactions == ["✅" if opened_with_it else "❌"]


def test_share_for_a_puzzle_that_gets_no_starter_ends_up_marked_so(tmp_path):
    bot, clock, channel = make_bot(tmp_path, hour=8, seed=False)  # a new install
    early = paste(bot, channel, 501, 1, TOMORROW_SHARE)
    assert early.reactions == ["⏳"]
    # The bot is down over post time, and a new install posts nothing late.
    clock.set(TOMORROW, 0, 1)
    asyncio.run(bot._tick())
    assert early.reactions == ["➖"]


def test_a_later_share_settles_earlier_pending_ones(tmp_path):
    bot, clock, channel = make_morning_bot(tmp_path, {})
    alice = paste(bot, channel, 501, 1, GOOD_SHARE)
    bot.service.source.words[TODAY] = SOLUTION
    clock.set(TODAY, 8, 30)
    assert paste(bot, channel, 502, 2, BAD_SHARE).reactions == ["❌"]
    assert alice.reactions == ["✅"]


def invoke(bot, name, *args, dms_open=True):
    interaction = Interaction()
    interaction.user.dms_open = dms_open
    asyncio.run(bot.tree.get_command(name).callback(interaction, *args))
    return interaction


def command(bot, name, *args):
    return "\n".join(invoke(bot, name, *args).user.dms)


def test_reports_refresh_a_missing_solution_first(tmp_path):
    bot, clock, channel = make_morning_bot(tmp_path, {})
    alice = paste(bot, channel, 501, 1, GOOD_SHARE)
    assert "No verified results" in command(bot, "results", None)

    bot.service.source.words[TODAY] = SOLUTION
    clock.set(TODAY, 8, 30)
    assert "user1: 2" in command(bot, "results", None)
    assert alice.reactions == ["✅"]


def test_leaderboard_refreshes_a_missing_solution_first(tmp_path):
    bot, clock, channel = make_morning_bot(tmp_path, {})
    paste(bot, channel, 501, 1, GOOD_SHARE)
    bot.service.source.words[TODAY] = SOLUTION
    clock.set(TODAY, 8, 30)
    assert "user1" in command(bot, "leaderboard", SimpleNamespace(value="weekly"))


# --- slash command acknowledgement -------------------------------------------

COMMANDS = [
    ("starter",),
    ("results", None),
    ("leaderboard", SimpleNamespace(value="weekly")),
    ("history", 10),
    ("config",),
    ("version",),
    ("reroll",),
]


def test_every_command_defers_before_doing_anything_else(tmp_path):
    bot, _, _ = make_bot(tmp_path)
    assert sorted(c.name for c in bot.tree.get_commands()) == sorted(c[0] for c in COMMANDS)
    for name, *args in COMMANDS:
        interaction = invoke(bot, name, *args)  # the fake raises on a second response
        assert interaction.events[0] == "defer", name
        assert interaction.events[-1] == "followup", name  # the spinner is resolved


def test_command_options_survive_the_defer_wrapper(tmp_path):
    bot, _, _ = make_bot(tmp_path)
    options = {c.name: [p.name for p in c.parameters] for c in bot.tree.get_commands()}
    assert options == {
        "starter": [],
        "results": ["puzzle"],
        "leaderboard": ["period"],
        "history": ["count"],
        "config": [],
        "version": [],
        "reroll": [],
    }


def test_long_output_is_sent_by_dm_then_confirmed(tmp_path):
    bot, _, _ = make_bot(tmp_path)
    bot.service.history = lambda count: "\n".join(["x" * 99] * 50)  # three chunks
    interaction = invoke(bot, "history", 100)
    assert interaction.events == ["defer", "dm", "dm", "dm", "followup"]
    assert "".join(interaction.user.dms) == bot.service.history(100)
    assert interaction.replies == ["Sent you a DM. 📬"]


def test_closed_dms_fall_back_to_ephemeral_followups(tmp_path):
    bot, _, _ = make_bot(tmp_path)
    bot.service.history = lambda count: "\n".join(["x" * 99] * 50)
    interaction = invoke(bot, "history", 100, dms_open=False)
    assert interaction.events == ["defer", "followup", "followup", "followup"]
    assert "".join(interaction.replies) == bot.service.history(100)


def test_a_failing_command_tells_the_user_and_logs(tmp_path, caplog):
    bot, _, _ = make_bot(tmp_path)

    def broken(count):
        raise RuntimeError("database is locked")

    bot.service.history = broken
    with caplog.at_level(logging.ERROR):
        interaction = invoke(bot, "history", 10)  # must not raise
    assert interaction.replies == [COMMAND_FAILED]
    assert "Command /history failed" in caplog.text and "database is locked" in caplog.text


def test_a_slow_refresh_does_not_hold_up_a_report(tmp_path, monkeypatch):
    bot, _, _ = make_bot(tmp_path)
    monkeypatch.setattr("wordlebot.bot.REPORT_REFRESH_SECONDS", 0.01)
    finished = []

    async def slow():
        await asyncio.sleep(0.05)
        finished.append(True)

    bot.service.refresh_if_missing = slow

    async def run():
        interaction = Interaction()
        await bot.tree.get_command("results").callback(interaction, None)
        assert not finished and interaction.replies == ["Sent you a DM. 📬"]
        await asyncio.gather(*bot._background)  # carries on in the background
        assert finished

    asyncio.run(run())


def test_bot_messages_cannot_ping_anyone(tmp_path):
    bot, _, _ = make_bot(tmp_path)
    allowed = bot.allowed_mentions
    assert (allowed.everyone, allowed.users, allowed.roles, allowed.replied_user) == (
        False,
        False,
        False,
        False,
    )


def test_version_command_dms_the_running_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("WORDLEBOT_COMMIT", "abc1234")
    bot, _, _ = make_bot(tmp_path)
    interaction = invoke(bot, "version")
    assert interaction.events == ["defer", "dm", "followup"]
    assert "commit `abc1234`" in interaction.user.dms[0]
    assert "discord.py" in interaction.user.dms[0]
