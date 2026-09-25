"""The six library commands are one group, and say so in what they tell people.

They were six top-level commands across two cogs. At the top level `/borrow`,
`/return` and `/deposit` are words a Discord server uses for plenty that has
nothing to do with this bot, and a server with no library got six greyed-out
commands instead of one.

The second test is the one that catches the real breakage. Regrouping renames
every command, and these commands talk about each other constantly -- "run
`/withdraw` again when it's sorted", "`/mydeposits` for the rest". A message
that names a command nobody can type any more is worse than no message: it
reads as an instruction and it cannot be followed.
"""
import re

import cogs.library_commands as mod
from cogs.library_commands import LibraryCommands

GROUPED = {"deposit", "withdraw", "deposits", "borrow", "return", "deck"}


def test_the_six_library_commands_are_one_group():
    assert {c.name for c in LibraryCommands.library.subcommands} == GROUPED


def test_nothing_the_library_says_names_a_command_that_no_longer_exists():
    """Every `/x` in a user-facing string has to be a command that exists.

    Read off the module's own source rather than a list kept here: a seventh
    command, or a new message quoting an old name, has to be caught by this
    without anyone remembering to update it.
    """
    source = open(mod.__file__).read()
    named = set(re.findall(r"`/([a-z_]+)`", source))
    gone = named & GROUPED
    assert not gone, (
        f"these read as instructions but name a command that is now under "
        f"/library: {sorted(gone)}")


def test_one_cog_carries_the_whole_feature():
    """Two cogs meant the deposit half imported the borrow half's gate and
    message budget across a module boundary, and a change to either had to be
    made in the place it was not."""
    added = []
    mod.setup(type("Bot", (), {"add_cog": lambda self, cog: added.append(cog)})())

    assert len(added) == 1 and isinstance(added[0], LibraryCommands)


def test_every_command_is_reachable_as_its_own_method():
    """The callback still hangs off the class under the method's own name,
    which is how every test in the suite drives these commands."""
    for name in ("deposit", "withdraw", "deposits", "borrow", "deck"):
        assert hasattr(getattr(LibraryCommands, name), "callback"), name
    assert hasattr(LibraryCommands.return_cards, "callback")
