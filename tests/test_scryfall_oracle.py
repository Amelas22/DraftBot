"""Oracle enrichment for the draft table page.

Draftmancer's carddata has no oracle text, no P/T and only a bare type. This
fills those in from Scryfall. It must degrade rather than raise: a page with
missing oracle text is fine, a publish that dies because Scryfall was slow is
not.
"""
import pytest

from helpers import scryfall_oracle


def test_faces_splits_a_double_faced_card():
    card = {"card_faces": [
        {"name": "Front", "mana_cost": "{1}{U}", "type_line": "Creature — Bird",
         "oracle_text": "Flying", "power": "1", "toughness": "3"},
        {"name": "Back", "mana_cost": "", "type_line": "Land", "oracle_text": "T: add U"},
    ]}

    faces = scryfall_oracle._faces(card)

    assert [f["name"] for f in faces] == ["Front", "Back"]
    assert faces[0]["pt"] == "1/3"
    assert faces[1]["pt"] == ""


def test_faces_wraps_a_single_faced_card_in_a_one_element_list():
    """One shape for the renderer to handle, never two."""
    card = {"name": "Bear", "type_line": "Creature — Bear",
            "oracle_text": "", "power": "2", "toughness": "2"}

    faces = scryfall_oracle._faces(card)

    assert len(faces) == 1
    assert faces[0]["pt"] == "2/2"


def test_faces_renders_loyalty_in_brackets():
    card = {"name": "Jace", "type_line": "Planeswalker", "loyalty": "3"}

    assert scryfall_oracle._faces(card)[0]["pt"] == "[3]"


@pytest.mark.asyncio
async def test_enrich_returns_a_face_list_per_resolved_id(monkeypatch):
    async def fake_fetch(ids, session=None):
        return {"data": [{"id": "abc", "name": "Bear", "type_line": "Creature",
                          "oracle_text": "", "power": "2", "toughness": "2"}],
                "not_found": []}
    monkeypatch.setattr(scryfall_oracle, "_fetch_collection", fake_fetch)

    got = await scryfall_oracle.enrich(["abc"])

    assert got["abc"]["faces"][0]["name"] == "Bear"


@pytest.mark.asyncio
async def test_enrich_batches_in_seventy_fives(monkeypatch):
    """Scryfall's /cards/collection takes at most 75 identifiers per POST."""
    seen = []

    async def fake_fetch(ids, session=None):
        seen.append(len(ids))
        return {"data": [], "not_found": []}
    monkeypatch.setattr(scryfall_oracle, "_fetch_collection", fake_fetch)

    await scryfall_oracle.enrich([f"id-{n}" for n in range(80)])

    assert seen == [75, 5]


@pytest.mark.asyncio
async def test_enrich_degrades_when_a_batch_fails(monkeypatch):
    """A Scryfall outage costs those cards their oracle text and nothing else."""
    async def boom(ids, session=None):
        raise TimeoutError("scryfall is down")
    monkeypatch.setattr(scryfall_oracle, "_fetch_collection", boom)

    got = await scryfall_oracle.enrich(["abc"])

    assert got == {}


@pytest.mark.asyncio
async def test_enrich_ignores_empty_ids(monkeypatch):
    async def fake_fetch(ids, session=None):
        raise AssertionError("should not be called")
    monkeypatch.setattr(scryfall_oracle, "_fetch_collection", fake_fetch)

    assert await scryfall_oracle.enrich(["", None]) == {}
