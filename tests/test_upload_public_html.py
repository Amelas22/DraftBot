"""Publishing a page that has to be readable by anyone with the link.

Every other upload in this helper is private and goes through the
bucket-specific client, which silently prefixes the bucket onto the key. A
public page cannot use that path: get_public_url would advertise a URL one
level shallower than where the object actually landed.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from helpers.digital_ocean_helper import DigitalOceanHelper


def _helper_with_spies():
    """A configured helper recording which client was used and what was sent."""
    helper = DigitalOceanHelper.__new__(DigitalOceanHelper)
    helper.logger = MagicMock()
    helper.bucket = "magic-draft-logs"
    helper.region = "nyc3"
    helper.config_valid = True

    s3 = MagicMock()
    s3.put_object = AsyncMock()
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=s3)
    client.__aexit__ = AsyncMock(return_value=None)

    helper.create_raw_client = AsyncMock(return_value=client)
    helper.create_client = AsyncMock(side_effect=AssertionError(
        "must use the raw client: the bucket endpoint double-nests the key"))
    return helper, s3


@pytest.mark.asyncio
async def test_uses_the_raw_client():
    helper, s3 = _helper_with_spies()

    await helper.upload_public_html("<html></html>", "drafttable", "x.html")

    helper.create_raw_client.assert_awaited()
    assert s3.put_object.await_args.kwargs["Key"] == "drafttable/x.html"


@pytest.mark.asyncio
async def test_is_served_as_html_not_plain_text():
    helper, s3 = _helper_with_spies()

    await helper.upload_public_html("<html></html>", "drafttable", "x.html")

    assert s3.put_object.await_args.kwargs["ContentType"] == "text/html; charset=utf-8"


@pytest.mark.asyncio
async def test_is_public_and_not_cached():
    """Published on purpose; and a rebuild under the same name must be seen."""
    helper, s3 = _helper_with_spies()

    await helper.upload_public_html("<html></html>", "drafttable", "x.html")

    assert s3.put_object.await_args.kwargs["ACL"] == "public-read"
    assert s3.put_object.await_args.kwargs["CacheControl"] == "no-cache"


@pytest.mark.asyncio
async def test_returns_the_object_path_on_success():
    helper, _ = _helper_with_spies()

    result = await helper.upload_public_html("<html></html>", "drafttable", "x.html")

    assert result.success is True
    assert result.object_path == "drafttable/x.html"


@pytest.mark.asyncio
async def test_reports_failure_rather_than_raising():
    helper, s3 = _helper_with_spies()
    s3.put_object = AsyncMock(side_effect=RuntimeError("spaces is down"))

    result = await helper.upload_public_html("<html></html>", "drafttable", "x.html")

    assert result.success is False
