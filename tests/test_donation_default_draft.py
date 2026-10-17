"""/draft asks nothing: the server's donation day decides which kind of draft it opens."""
import inspect

from cogs.draft_commands import DraftCommands
from services.donation_day import draft_kind


def test_a_running_donation_day_opens_a_donation_draft():
    assert draft_kind(money_server=True, event_active=True) == "donation"


def test_without_a_donation_day_a_money_server_opens_a_staked_draft():
    assert draft_kind(money_server=True, event_active=False) == "staked"


def test_a_free_server_never_opens_a_money_draft():
    assert draft_kind(money_server=False, event_active=True) is None
    assert draft_kind(money_server=False, event_active=False) is None


def test_draft_takes_no_options():
    """An option shows in every player's command picker, event or not."""
    params = inspect.signature(DraftCommands.draft.callback).parameters
    assert list(params) == ["self", "ctx"]
    # Before the cog is loaded py-cord reads ctx itself as an option; no other may exist.
    assert [o.name for o in DraftCommands.draft.options] in ([], ["ctx"])
