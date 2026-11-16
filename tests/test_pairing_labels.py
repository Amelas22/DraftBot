"""MTGO handles on pairing embeds: shown only when the Discord name doesn't already say it."""
from unittest.mock import MagicMock

import pytest

from conftest import test_db  # noqa: F401  (fixture)
from helpers.pairing_labels import pairing_label, pairing_labels, reported_field, unplayed_field
from models.mtgo_account import MtgoAccount


def label(name, handle):
    return pairing_label(name, name, handle)


class TestWhenTheHandleIsShown:
    @pytest.mark.parametrize("name, handle", [
        ("aberdasher", "aber"),               # the handle is in the name
        ("aber", "aberdasher"),               # the name is in the handle
        ("gregg / keezles", "keezles"),       # two names, one of them the handle
        ("Jeremy (amelas)", "Amelas"),
        ("LSV", "lsv_mtg"),                   # case and punctuation ignored
        ("Mr. Smith", "mrsmith"),
        ("Al", "al"),                         # short names still match exactly
    ])
    def test_hidden_when_one_name_contains_the_other(self, name, handle):
        assert label(name, handle) == name

    @pytest.mark.parametrize("name, handle", [
        ("aylin", "giraffesarentreal"),
        ("aylin (she/her)", "giraffesarentreal"),
        ("dancingjersey (Kodie)", "kodieyost"),   # a second name that isn't the handle
        ("screemers", "screamers"),               # a typo apart is still not the handle
        ("al", "albatross"),                      # too short to count as contained
        ("🔥", "anything"),                       # nothing left to compare
        ("[AFK] aber", "aberdasher"),             # "afkaber" is not in "aberdasher"
    ])
    def test_shown_otherwise(self, name, handle):
        assert label(name, handle) == f"{name} (MTGO: {handle})"

    def test_unlinked_never(self):
        assert label("aylin", None) == "aylin"


class TestPairingLabel:
    def test_suffix_appended_and_escaped(self):
        # `shown` is already escaped/iconed upstream; the handle must be escaped here,
        # or an underscore in it italicises the rest of the field.
        assert pairing_label("👑 aylin", "aylin", "spy_glass_") == "👑 aylin (MTGO: spy\\_glass\\_)"

    def test_handle_cannot_mention(self):
        assert "@\u200beveryone" in pairing_label("aylin", "aylin", "@everyone")

    def test_shown_name_is_kept_verbatim(self):
        # Similarity is judged on the raw name; `shown` may carry icons and escapes.
        assert pairing_label("👑 aber\\_dasher", "aber_dasher", "aber") == "👑 aber\\_dasher"


def test_field_shapes():
    assert unplayed_field(3, "A", "B (MTGO: b)") == "⚫ **Match 3**\nA v.\nB (MTGO: b)"
    assert reported_field("🔴 ", 3, "A", 2, "B (MTGO: b)", 1) == (
        "🔴 **Match 3**\nA: 2 wins\nB (MTGO: b): 1 wins")


def _guild(names):
    guild = MagicMock()
    guild.id = 1

    def get_member(uid):
        if uid not in names:
            return None
        member = MagicMock()
        member.display_name = names[uid]
        member.roles = []
        return member
    guild.get_member.side_effect = get_member
    return guild


@pytest.mark.asyncio
async def test_pairing_labels_looks_up_linked_handles(test_db):
    await MtgoAccount.link(1, "Spy_Glass")      # aylin: unrelated -> shown
    await MtgoAccount.link(2, "aber")           # aberdasher: similar -> hidden
    await MtgoAccount.link(3, "keezles")        # second name is the handle -> hidden
    guild = _guild({1: "aylin", 2: "aberdasher", 3: "gregg/keezles", 4: "unlinked"})

    labels = await pairing_labels(guild, ["1", "2", "3", "4"])

    assert labels == {
        "1": "aylin (MTGO: Spy\\_Glass)",
        "2": "aberdasher",
        "3": "gregg/keezles",
        "4": "unlinked",
    }


@pytest.mark.asyncio
async def test_departed_member_shows_by_signup_name(test_db):
    await MtgoAccount.link(5, "gone_player")
    await MtgoAccount.link(6, "Kodie")
    sign_ups = {"5": "aylin", "6": "kodie_y", "7": "Fenn_x"}
    labels = await pairing_labels(_guild({}), ["5", "6", "7"], sign_ups)
    assert labels == {
        "5": "aylin (MTGO: gone\\_player)",   # unrelated handle still shown
        "6": "kodie\\_y",                      # similar handle hidden, name escaped
        "7": "Fenn\\_x",                       # unlinked
    }


@pytest.mark.asyncio
async def test_departed_member_without_signup_name(test_db):
    await MtgoAccount.link(5, "gone_player")
    labels = await pairing_labels(_guild({}), ["5"])
    assert labels == {"5": "Unknown User (MTGO: gone\\_player)"}


@pytest.mark.asyncio
async def test_missing_player_id_renders_as_unknown(test_db):
    labels = await pairing_labels(_guild({}), [None, "1"])
    assert labels == {"None": "Unknown User", "1": "Unknown User"}


@pytest.mark.asyncio
async def test_failed_lookup_falls_back_to_discord_names(test_db, monkeypatch):
    async def boom(_ids):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(MtgoAccount, "usernames_for_discord_ids", boom)
    await MtgoAccount.link(1, "Spy_Glass")
    assert await pairing_labels(_guild({1: "aylin"}), ["1"]) == {"1": "aylin"}
