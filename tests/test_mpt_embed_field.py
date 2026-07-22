from services.draft_setup_manager import mpt_embed_field_value


def test_mpt_embed_field_value_link_when_url():
    assert mpt_embed_field_value("https://magicprotools.com/draft/show?id=X") == \
        "[View on MagicProTools](https://magicprotools.com/draft/show?id=X)"


def test_mpt_embed_field_value_unavailable_note_when_none():
    assert mpt_embed_field_value(None) == "_log temporarily unavailable_"
