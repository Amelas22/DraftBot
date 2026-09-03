from collections import namedtuple
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers.room_regeneration import (
    carried_over_member_ids,
    regenerate_team_rooms,
    remaining_channel_ids,
    select_team_rooms,
    team_plan,
)

FID = "reckless-crew-92"

# A role also carries an overwrite on a team room (Scryfall). The guild answers
# get_member(None) for it, which is how roles get filtered back out.
SCRYFALL_ROLE_ID = "1362296581482872855"
SUB_ID = "144605755826372608"

# Overwrite dicts are keyed by the Role/Member object, so the double must hash.
_Target = namedtuple("_Target", "id")


def _draft_rooms():
    """The five rooms a premade draft makes, as pycord would report them --
    Discord lowercases text channel names and leaves voice names as sent.
    """
    # A draft whose rooms overflowed out of the configured category, which is
    # the case where placement is observable at all.
    overflow = SimpleNamespace(name="Draft Channels 2")
    voice_cat = SimpleNamespace(name="Draft Voice")
    # The team chat carries an overwrite for a substitute who is in no roster,
    # plus a bot role -- exactly what /add_sub leaves behind.
    red_chat_overwrites = {
        _Target("101"): None,
        _Target(SUB_ID): None,
        _Target(SCRYFALL_ROLE_ID): None,
    }
    return [
        SimpleNamespace(id=1, name=f"draft-chat-{FID}", category=overflow, overwrites={}),
        SimpleNamespace(id=2, name=f"red-team-chat-{FID}", category=overflow,
                        overwrites=red_chat_overwrites),
        SimpleNamespace(id=3, name=f"Red-Team-Voice-{FID}", category=voice_cat, overwrites={}),
        SimpleNamespace(id=4, name=f"blue-team-chat-{FID}", category=overflow, overwrites={}),
        SimpleNamespace(id=5, name=f"Blue-Team-Voice-{FID}", category=voice_cat, overwrites={}),
    ]


def _session(**overrides):
    session = SimpleNamespace(
        session_id="548842932065665025-1788217149",
        friendly_id=FID,
        session_type="premade",
        session_stage="pairings",
        draft_chat_channel="1",
        team_a=["101", "102", "103"],
        team_b=["201", "202", "203"],
        sign_ups={},
        channel_ids=[1, 2, 3, 4, 5],
    )
    for key, value in overrides.items():
        setattr(session, key, value)
    return session


def _guild():
    """A guild holding the five rooms of a finished premade draft.

    get_member answers only for real player ids, so a role id passed in with
    them comes back None -- which is how the production code tells them apart.
    """
    channels = {c.id: c for c in _draft_rooms()}
    deleted = []
    for channel in channels.values():
        channel.delete = AsyncMock(side_effect=lambda c=channel, **_: deleted.append(c.id))
    members = {"101", "102", "103", "201", "202", "203", SUB_ID}
    return SimpleNamespace(
        id=1355718878298116096,
        channels=channels,
        deleted=deleted,
        get_channel=lambda cid: channels.get(int(cid)),
        get_member=lambda uid: (SimpleNamespace(id=str(uid), display_name=f"p{uid}")
                                if str(uid) in members else None),
    )


class _FakeView:
    """Stands in for PersistentView, keeping the one part of its contract that
    matters here: __init__ does NOT set `draft_chat_channel`, and
    create_team_channel reads it unconditionally when it writes the row
    (views.py). A double that quietly tolerated the missing attribute would hide
    the crash this exists to prevent.
    """

    def __init__(self, events, created):
        self._events = events
        self._created = created
        self.channel_ids = []

    async def create_team_channel(self, *args, **kwargs):
        _ = self.draft_chat_channel          # AttributeError unless seeded
        self._events.append(("create", None))
        self._created.update(kwargs)
        self._created["members"] = args[2]
        return 99


async def _run(session, guild, team_name, create_error=None):
    """Drive a regenerate against fakes.

    Returns (result, error, events, persisted, created) -- `events` records the
    order of the two steps whose sequence is the whole point, `persisted` merges
    every column write, `created` captures the rebuild call.
    """
    events, persisted, created = [], {}, {}

    async def fake_persist(session_id, **values):
        events.append(("persist", values))
        persisted.update(values)

    view = _FakeView(events, created)
    if create_error is not None:
        view.create_team_channel = AsyncMock(side_effect=create_error)

    with patch("helpers.room_regeneration.get_draft_session", AsyncMock(return_value=session)), \
         patch("helpers.room_regeneration._persist", fake_persist), \
         patch("views.PersistentView", MagicMock(return_value=view)), \
         patch("services.draft_log_store.post_team_logs", AsyncMock(return_value=True)), \
         patch("utils.post_pairings", AsyncMock()):
        result, error = await regenerate_team_rooms(
            object(), guild, session.session_id, team_name)
    return result, error, events, persisted, created


def test_selects_only_the_named_teams_chat_and_voice():
    assert [c.id for c in select_team_rooms(_draft_rooms(), "Red-Team", FID)] == [2, 3]


def test_forgetting_the_stale_rooms_leaves_the_other_teams_rooms():
    # channel_ids is JSON, so ids come back as ints here and strings there;
    # a regenerate that missed one would hand the broken channel straight back.
    assert remaining_channel_ids([1, "2", 3, 4, "5"], [2, 3]) == [1, 4, "5"]


@pytest.mark.parametrize("team_name,members,field", [
    ("Red-Team", ["101", "102", "103"], "team_a_pools_destination_id"),
    ("Blue-Team", ["201", "202", "203"], "team_b_pools_destination_id"),
])
def test_team_plan_pairs_a_roster_with_the_pools_it_owns(team_name, members, field):
    plan = team_plan(_session(), team_name)
    assert plan.member_ids == members
    assert plan.pools_field == field


def test_the_shared_chat_has_every_player_and_the_open_pools_thread():
    plan = team_plan(_session(), "Draft")
    assert plan.member_ids == ["101", "102", "103", "201", "202", "203"]
    assert plan.pools_field == "open_pools_destination_id"


def test_a_team_less_draft_takes_its_shared_roster_from_sign_ups():
    """Swiss has no team_a/team_b and only one room. Falling back to the empty
    rosters would rebuild it with no members and lock the draft out entirely."""
    swiss = _session(team_a=[], team_b=[], sign_ups={"301": "Ana", "302": "Bo"})
    assert team_plan(swiss, "Draft").member_ids == ["301", "302"]


def test_the_rooms_own_overwrites_name_the_substitute_the_roster_does_not():
    """/add_sub never writes the sub into team_a/team_b, so the room's own
    overwrites are the only record that they were given access."""
    red_chat = _draft_rooms()[1]
    assert SUB_ID in carried_over_member_ids(red_chat)


@pytest.mark.asyncio
async def test_the_stale_rooms_are_forgotten_before_the_new_one_is_made():
    """The ordering IS the fix. create_team_channel reuses any channel whose id
    the draft still owns, so a regenerate that created before it forgot would
    hand back the broken channel and repair nothing.
    """
    _result, _error, events, persisted, _created = await _run(
        _session(), _guild(), "Red-Team")

    assert [name for name, _ in events] == ["persist", "create"]
    assert persisted["channel_ids"] == [1, 4, 5]


@pytest.mark.asyncio
async def test_the_substitute_keeps_access_across_the_rebuild():
    """The whole point of the command. The sub is in no roster, so only the old
    room's overwrites carry them into the new room's creation payload."""
    _r, _e, _ev, _p, created = await _run(_session(), _guild(), "Red-Team")
    assert SUB_ID in [m.id for m in created["members"]]


@pytest.mark.asyncio
async def test_roles_on_the_old_room_are_not_mistaken_for_members():
    _r, _e, _ev, _p, created = await _run(_session(), _guild(), "Red-Team")
    assert SCRYFALL_ROLE_ID not in [m.id for m in created["members"]]


@pytest.mark.asyncio
async def test_only_the_named_teams_rooms_are_deleted():
    guild = _guild()
    await _run(_session(), guild, "Red-Team")
    # The shared chat and Blue's rooms are in use by people who are not affected.
    assert guild.deleted == [2, 3]


@pytest.mark.asyncio
async def test_the_rooms_pools_are_cleared_so_post_team_logs_reopens_them():
    _r, _e, _ev, persisted, _c = await _run(_session(), _guild(), "Red-Team")
    assert persisted["team_a_pools_destination_id"] is None
    assert persisted["team_logs_posted_at"] is None
    # Blue's thread is alive and still holds its pools; nulling it would make
    # post_team_logs open a second, duplicate one.
    assert "team_b_pools_destination_id" not in persisted


@pytest.mark.asyncio
async def test_a_team_with_no_rooms_is_reported_rather_than_rebuilt():
    session = _session(channel_ids=[1])   # shared chat only; Red's rooms are gone
    result, error, events, _persisted, _c = await _run(session, _guild(), "Red-Team")
    assert result is None
    assert "no rooms recorded" in error
    assert events == []


@pytest.mark.asyncio
async def test_a_failed_delete_still_forgets_the_rooms_it_did_delete():
    """The chat can go and the voice channel then refuse. Leaving the deleted
    room in channel_ids points the draft at something that no longer exists --
    and reporting "nothing was changed" would be false in the one case an admin
    most needs the truth.
    """
    guild = _guild()
    guild.channels[3].delete = AsyncMock(side_effect=RuntimeError("nope"))

    result, error, _events, persisted, _c = await _run(_session(), guild, "Red-Team")

    assert result is None
    assert persisted["channel_ids"] == [1, 3, 4, 5]      # 2 went, 3 is still there
    assert "Deleted 1 of 2" in error


@pytest.mark.asyncio
async def test_a_failed_rebuild_says_the_rooms_are_gone():
    """Past the destructive step, an exception must not reach the slash command
    as a generic interaction failure -- the rooms no longer exist."""
    result, error, _ev, _p, _c = await _run(
        _session(), _guild(), "Red-Team", create_error=RuntimeError("discord said no"))
    assert result is None
    assert "rooms are gone" in error


@pytest.mark.asyncio
async def test_the_new_room_lands_beside_the_rooms_it_is_replacing():
    """A busy guild overflows its draft category into a numbered sibling. Letting
    create_team_channel pick a category afresh would put the rebuilt room back in
    the configured one, separated from this draft's other rooms -- in a repair
    whose whole premise is that a player cannot find their room.
    """
    _r, _e, _ev, _p, created = await _run(_session(), _guild(), "Red-Team")
    assert created["rooms_category"].name == "Draft Channels 2"


@pytest.mark.asyncio
async def test_the_category_comes_from_the_text_room_not_the_voice_one():
    """stale follows channel_ids order. Probing whichever sorted first would hand
    the rebuilt TEXT channel the guild's voice category."""
    session = _session(channel_ids=[3, 2, 1, 4, 5])   # voice first
    _r, _e, _ev, _p, created = await _run(session, _guild(), "Red-Team")
    assert created["rooms_category"].name == "Draft Channels 2"


@pytest.mark.asyncio
async def test_a_finished_draft_is_not_rewound_to_pairings():
    """create_team_channel is the setup path and stamps 'pairings' unconditionally.
    Rewinding a completed draft puts it back in front of the live-draft
    re-register and the log reconciler."""
    session = _session(session_stage="completed")
    _r, _e, _ev, persisted, _c = await _run(session, _guild(), "Red-Team")
    assert persisted["session_stage"] == "completed"
