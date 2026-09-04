"""Build a draft's table page and publish it, or return None.

Called from publish_draft_log before the embed is sent, because the URL has to
go into that embed. The embed is the deliverable and the page is an enhancement
to it, so every failure here is swallowed and reported as None: a draft that
loses its page still gets its log posted.

There is deliberately no retry. Of the ways this can fail, only a transient
Spaces upload would behave differently on a second attempt -- and the
production logs show 836 successful uploads with none failing. Everything else
(a malformed log, an unexpected booster shape) is deterministic, so a retry
would burn the same error again. See the design doc.
"""
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select

from database.db_session import db_session
from helpers.digital_ocean_helper import DigitalOceanHelper
from models.draft_session import DraftSession
from services import draft_table_page

SPACES_FOLDER = "drafttable"


async def resolve_filename(draft_session: Any) -> str:
    """`<friendly_id>.html`, suffixed if another draft already published it.

    friendly_id is not unique -- festering-newt-77 and songstitcher-23 each
    occur twice across the history -- and the filename becomes a permanent
    public URL, so a second draft with the same name must not overwrite the
    first. Checked against the recorded URLs rather than against Spaces: one
    indexed read instead of a network round-trip.
    """
    friendly = draft_session.friendly_id or draft_session.session_id
    base = f"{friendly}.html"

    async with db_session() as session:
        clash = (await session.execute(
            select(DraftSession.session_id).where(
                DraftSession.drafttable_url.like(f"%/{base}"),
                DraftSession.session_id != draft_session.session_id,
            )
        )).scalars().first()

    if not clash:
        return base
    # The Draftmancer draft id tail is unique per draft, so one suffix is enough.
    suffix = str(draft_session.session_id)[-6:]
    logger.warning(f"drafttable: {base} already published by {clash}; "
                   f"using {friendly}-{suffix}.html")
    return f"{friendly}-{suffix}.html"


async def publish(draft_data: dict[str, Any], draft_session: Any) -> Optional[str]:
    """Render, upload, and return the public URL -- or None if anything failed."""
    try:
        meta = draft_table_page.session_meta_from(draft_session, draft_data)
        html = await draft_table_page.render(draft_data, meta)
        filename = await resolve_filename(draft_session)

        helper = DigitalOceanHelper()
        result = await helper.upload_public_html(html, SPACES_FOLDER, filename)
        if not result.success or result.object_path is None:
            logger.warning(f"drafttable: upload failed for {draft_session.session_id}")
            return None

        url = helper.get_public_url(result.object_path)
        logger.info(f"drafttable: published {url} ({len(html):,} chars)")
        return url
    except Exception as e:
        logger.exception(
            f"drafttable: could not publish for {draft_session.session_id}: {e}")
        return None
