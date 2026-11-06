"""Unit tests for DraftSetupManager.capture_draft_log (Slice 1)."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.draft_setup_manager import DraftSetupManager


def _manager():
    """A DraftSetupManager without running __init__ (skips socket setup)."""
    m = DraftSetupManager.__new__(DraftSetupManager)
    m.session_id = "sid"
    m.draft_id = "ABC123"
    m.bot_user_id = "DraftBot"  # normally set by __init__; used when connecting
    m.cube_id = "MyCube"
    m.session_type = "premade"
    m.guild_id = "42"
    m.logger = MagicMock()
    m._disconnect_notice_task = None  # _on_end_draft drops any pending disconnect notice
    return m


def _draft_data():
    return {
        "sessionID": "DBABC123",
        "time": 1700000000000,
        "users": {
            "u1": {"userName": "Alice", "seatNum": 0, "picks": [{"booster": [1]}]},
            "u2": {"userName": "Bob", "seatNum": 1, "picks": [{"booster": [2]}]},
        },
    }


def _mock_db_session(draft_session):
    """Patch target for `db_session()` -> async ctx mgr yielding a session whose
    execute().scalar_one_or_none() returns `draft_session`."""
    session = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = draft_session
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return MagicMock(return_value=ctx), session


def _uncaptured(**over):
    """A session row as it stands when the draft ends: nothing captured yet."""
    ds = SimpleNamespace(session_id="sid", sign_ups={"d1": "Alice", "d2": "Bob"},
                         draft_data=None, pack_first_picks=None, logs_captured_at=None,
                         unlock_at=None, data_received=False, tournament_match_id=None)
    for k, v in over.items():
        setattr(ds, k, v)
    return ds


@pytest.mark.asyncio
async def test_capture_stores_data_and_stamps_without_publishing():
    m = _manager()
    ds = _uncaptured()
    db_factory, session = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch.object(DraftSetupManager, "save_to_digitalocean_spaces", AsyncMock(return_value="team/x.json")) as spaces, \
         patch.object(DraftSetupManager, "get_pack_first_picks", MagicMock(return_value={})), \
         patch.object(DraftSetupManager, "send_magicprotools_embed", AsyncMock()) as embed:
        ok = await m.capture_draft_log(_draft_data())

    assert ok is True
    spaces.assert_awaited_once()
    embed.assert_not_called()
    assert ds.data_received is False
    assert ds.draft_data is not None
    assert ds.logs_captured_at is not None
    assert ds.unlock_at is not None
    assert ds.unlock_at > ds.logs_captured_at
    assert ds.spaces_object_key == "team/x.json"
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_capture_unlocks_at_once_for_a_tournament_match():
    """A tournament match is played with pools open, and the public log says
    nothing the pools don't -- so its publish gate is the capture time itself,
    not three hours later."""
    m = _manager()
    ds = _uncaptured(tournament_match_id=7)
    db_factory, _ = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch.object(DraftSetupManager, "save_to_digitalocean_spaces", AsyncMock(return_value="team/x.json")), \
         patch.object(DraftSetupManager, "get_pack_first_picks", MagicMock(return_value={})):
        ok = await m.capture_draft_log(_draft_data())

    assert ok is True
    assert ds.logs_captured_at is not None
    assert ds.unlock_at == ds.logs_captured_at


@pytest.mark.asyncio
async def test_capture_is_idempotent():
    m = _manager()
    ds = SimpleNamespace(session_id="sid", sign_ups={}, draft_data=None,
                         pack_first_picks=None, logs_captured_at=datetime.now(),
                         data_received=False)
    db_factory, _ = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch.object(DraftSetupManager, "save_to_digitalocean_spaces", AsyncMock(return_value="team/x.json")) as spaces:
        ok = await m.capture_draft_log(_draft_data())

    assert ok is True
    spaces.assert_not_called()


@pytest.mark.asyncio
async def test_capture_no_data_is_noop():
    m = _manager()
    with patch.object(DraftSetupManager, "save_to_digitalocean_spaces", AsyncMock()) as spaces:
        ok = await m.capture_draft_log(None)
    assert ok is False
    spaces.assert_not_called()


@pytest.mark.asyncio
async def test_capture_spaces_failure_keeps_db_copy_without_stamp():
    """If the Spaces upload fails, the raw log is still saved to the DB, but
    logs_captured_at stays NULL so the capture is retryable."""
    m = _manager()
    ds = SimpleNamespace(session_id="sid", sign_ups={"d1": "Alice", "d2": "Bob"},
                         draft_data=None, pack_first_picks=None, logs_captured_at=None,
                         data_received=False, spaces_object_key=None)
    db_factory, session = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch.object(DraftSetupManager, "save_to_digitalocean_spaces", AsyncMock(return_value=None)) as spaces, \
         patch.object(DraftSetupManager, "get_pack_first_picks", MagicMock(return_value={})), \
         patch.object(DraftSetupManager, "send_magicprotools_embed", AsyncMock()) as embed:
        ok = await m.capture_draft_log(_draft_data())

    assert ok is False                      # upload failed
    spaces.assert_awaited_once()
    embed.assert_not_called()
    assert ds.draft_data is not None        # raw log still saved (data safety)
    assert ds.logs_captured_at is None      # NOT stamped -> retryable
    assert ds.spaces_object_key is None     # upload failed -> no key, stays retryable


@pytest.mark.asyncio
async def test_on_end_draft_captures_log():
    """A naturally-completed draft triggers capture_draft_log with the pushed log."""
    m = _manager()
    m.draft_cancelled = False
    m.drafting = True
    m.draftPaused = False
    m.draft_channel_id = "999"
    m.current_draft_log = _draft_data()
    bot = MagicMock()
    bot.get_guild.return_value = None      # skip the rooms-creation branch
    bot.get_channel.return_value = None
    with patch("services.draft_setup_manager.get_bot", return_value=bot), \
         patch.object(DraftSetupManager, "capture_draft_log", AsyncMock()) as cap:
        await m._on_end_draft()
    cap.assert_awaited_once_with(m.current_draft_log)


@pytest.mark.asyncio
async def test_on_end_draft_warns_when_no_log_arrives():
    """If the draftLog push never lands, the wait loop times out, logs a warning,
    and does NOT call capture (no data to capture)."""
    from services.draft_setup_manager import DRAFT_LOG_WAIT_ATTEMPTS
    m = _manager()
    m.draft_cancelled = False
    m.draft_channel_id = "999"
    m.current_draft_log = None
    bot = MagicMock()
    bot.get_guild.return_value = None
    bot.get_channel.return_value = None
    with patch("services.draft_setup_manager.get_bot", return_value=bot), \
         patch("services.draft_setup_manager.asyncio.sleep", AsyncMock()) as sleep, \
         patch.object(DraftSetupManager, "capture_draft_log", AsyncMock()) as cap:
        await m._on_end_draft()
    cap.assert_not_awaited()
    assert sleep.await_count == DRAFT_LOG_WAIT_ATTEMPTS
    m.logger.warning.assert_called()


@pytest.mark.asyncio
async def test_publish_posts_and_marks_received():
    m = _manager()
    ds = SimpleNamespace(session_id="sid", draft_data=_draft_data(), data_received=False, drafttable_url=None)
    db_factory, _ = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch.object(DraftSetupManager, "send_magicprotools_embed", AsyncMock(return_value=True)) as embed:
        ok = await m.publish_draft_log()
    assert ok is True
    embed.assert_awaited_once()
    assert ds.data_received is True


@pytest.mark.asyncio
async def test_publish_does_not_mark_received_when_embed_not_sent():
    """If send_magicprotools_embed reports it didn't actually send anything
    (guild/channel missing, or an exception was swallowed), publish_draft_log
    must NOT stamp data_received, so the reconciler retries on a later tick."""
    m = _manager()
    ds = SimpleNamespace(session_id="sid", draft_data=_draft_data(), data_received=False, drafttable_url=None)
    db_factory, _ = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch.object(DraftSetupManager, "send_magicprotools_embed", AsyncMock(return_value=False)) as embed:
        ok = await m.publish_draft_log()
    assert ok is False
    embed.assert_awaited_once()
    assert ds.data_received is False


@pytest.mark.asyncio
async def test_publish_idempotent_when_already_received():
    m = _manager()
    ds = SimpleNamespace(session_id="sid", draft_data=_draft_data(), data_received=True, drafttable_url=None)
    db_factory, _ = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch.object(DraftSetupManager, "send_magicprotools_embed", AsyncMock()) as embed:
        ok = await m.publish_draft_log()
    assert ok is True
    embed.assert_not_called()


@pytest.mark.asyncio
async def test_publish_no_data_returns_false():
    m = _manager()
    ds = SimpleNamespace(session_id="sid", draft_data=None, data_received=False, drafttable_url=None)
    db_factory, _ = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch.object(DraftSetupManager, "send_magicprotools_embed", AsyncMock()) as embed:
        ok = await m.publish_draft_log()
    assert ok is False
    embed.assert_not_called()


@pytest.mark.asyncio
async def test_publish_release_emits_sharedraftlog_when_connected():
    m = _manager()
    m.current_draft_log = _draft_data()
    m.socket_client = MagicMock()
    m.socket_client.connected = True
    m.socket_client.emit = AsyncMock()
    ds = SimpleNamespace(session_id="sid", draft_data=_draft_data(), data_received=False, drafttable_url=None)
    db_factory, _ = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch.object(DraftSetupManager, "send_magicprotools_embed", AsyncMock()):
        ok = await m.publish_draft_log(release=True)
    assert ok is True
    m.socket_client.emit.assert_awaited_once()
    assert m.socket_client.emit.await_args.args[0] == "shareDraftLog"
    assert m.socket_client.emit.await_args.args[1].get("delayed") is False


@pytest.mark.asyncio
async def test_publish_release_skips_when_disconnected():
    m = _manager()
    m.current_draft_log = _draft_data()
    m.socket_client = MagicMock()
    m.socket_client.connected = False
    m.socket_client.emit = AsyncMock()
    ds = SimpleNamespace(session_id="sid", draft_data=_draft_data(), data_received=False, drafttable_url=None)
    db_factory, _ = _mock_db_session(ds)
    with patch("services.draft_setup_manager.db_session", db_factory), \
         patch.object(DraftSetupManager, "send_magicprotools_embed", AsyncMock()) as embed:
        ok = await m.publish_draft_log(release=True)
    assert ok is True
    m.socket_client.emit.assert_not_called()
    embed.assert_awaited_once()
    assert ds.data_received is True


async def _run_end_draft(unlock_at, pools_posted=True):
    """Run _on_end_draft with its collaborators mocked; the row the manager
    reads back after capture says the log unlocks at `unlock_at`."""
    m = _manager()
    m.draft_cancelled = False
    m.draft_channel_id = "999"
    m.current_draft_log = _draft_data()
    bot = MagicMock()
    bot.get_guild.return_value = None
    bot.get_channel.return_value = None
    state = SimpleNamespace(data_received=False, unlock_at=unlock_at)
    with patch("services.draft_setup_manager.get_bot", return_value=bot), \
         patch.object(DraftSetupManager, "capture_draft_log", AsyncMock()), \
         patch.object(DraftSetupManager, "_load_log_state", AsyncMock(return_value=state)), \
         patch.object(DraftSetupManager, "release_draftmancer_log", AsyncMock()) as release, \
         patch.object(DraftSetupManager, "publish_draft_log", AsyncMock()) as pub, \
         patch("services.draft_setup_manager.post_team_logs", AsyncMock(return_value=pools_posted)) as team, \
         patch("services.draft_setup_manager.assign_drafted_decks", AsyncMock()) as decks:
        await m._on_end_draft()
    return SimpleNamespace(m=m, bot=bot, team=team, decks=decks, release=release, pub=pub)


@pytest.mark.asyncio
async def test_on_end_draft_posts_team_pools():
    run = await _run_end_draft(unlock_at=datetime(2099, 1, 1))
    run.team.assert_awaited_once_with(run.m.session_id, run.bot)


@pytest.mark.asyncio
async def test_on_end_draft_releases_draftmancer_when_capture_made_the_log_due():
    """A tournament match's capture makes its log due at once (see
    capture_draft_log). The manager shuts down within seconds of unlock_at
    passing, so Draftmancer is unlocked here, now, over this socket; the
    Discord post is the reconciler's on its next tick, not an eager publish."""
    run = await _run_end_draft(unlock_at=datetime(2020, 1, 1))
    run.release.assert_awaited_once_with()
    run.pub.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_end_draft_leaves_an_ordinary_draft_locked_until_its_timer():
    run = await _run_end_draft(unlock_at=datetime(2099, 1, 1))
    run.release.assert_not_awaited()
    run.pub.assert_not_awaited()


@pytest.mark.asyncio
async def test_manually_unlock_delegates_to_publish_with_release():
    m = _manager()
    with patch.object(DraftSetupManager, "publish_draft_log", AsyncMock(return_value=True)) as pub:
        result = await m.manually_unlock_draft_logs()
    assert result is True
    pub.assert_awaited_once_with(release=True)


@pytest.mark.asyncio
async def test_keep_connection_alive_reclaims_ownership_after_initial_connect():
    """A manager spawned by the capture-retry (spawn_for_existing_session ->
    keep_connection_alive) must re-assert itself as session owner right after
    the initial connect, mirroring _handle_reconnection, otherwise on an
    inactive/ended Draftmancer session it never receives the owner log push."""
    m = _manager()
    m.draft_id = "ABC123"
    m._should_disconnect = True  # break out of the while-loop on first iteration
    m.socket_client = MagicMock()
    m.socket_client.connected = True
    m.socket_client.connect_with_retry = AsyncMock(return_value=True)
    m.socket_client.disconnect = AsyncMock()
    m._reclaim_ownership_as_spectator = AsyncMock(return_value=True)
    m.disconnect_safely = AsyncMock()
    with patch("services.draft_setup_manager.get_draftmancer_websocket_url", return_value="ws://x"), \
         patch("services.draft_setup_manager.asyncio.sleep", AsyncMock()):
        await m.keep_connection_alive()
    m._reclaim_ownership_as_spectator.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_to_spaces_returns_object_key():
    m = DraftSetupManager.__new__(DraftSetupManager)   # bypass __init__
    m.session_type = "team"; m.cube_id = "TestCube"
    m.logger = MagicMock()
    fake_result = MagicMock(success=True, object_path="team/TestCube-123-DBABC.json")
    fake_helper = MagicMock(); fake_helper.upload_json = AsyncMock(return_value=fake_result)
    with patch("services.draft_setup_manager.DigitalOceanHelper", return_value=fake_helper):
        key = await m.save_to_digitalocean_spaces({"time": 123, "sessionID": "DBABC"})
    assert key == "team/TestCube-123-DBABC.json"


# --- A draft stopped and restarted produces a SECOND log --------------------
#
# The first run's log had been captured, so the second arrived to a guard that
# only asked "was anything captured?" and was dropped: pools were posted from
# two-pick pools, and deck assignment, having judged that log unseatable,
# refused the real one for the rest of the process.


def _captured(draft_data, **over):
    ds = SimpleNamespace(session_id="sid", sign_ups={"d1": "Alice", "d2": "Bob"},
                         draft_data=draft_data, pack_first_picks={"d1": ["old"]},
                         logs_captured_at=datetime(2026, 11, 5, 22, 8),
                         unlock_at=datetime(2026, 11, 6, 1, 8),
                         team_logs_posted_at=datetime(2026, 11, 5, 22, 27),
                         drafttable_url="https://tables/first",
                         spaces_object_key="team/first.json", data_received=False,
                         tournament_match_id=None)
    for k, v in over.items():
        setattr(ds, k, v)
    return ds


def _second_run():
    log = _draft_data()
    log["time"] = _draft_data()["time"] + 7 * 60 * 1000
    return log


def _supersede_patches(spaces_key, dropped=()):
    return (
        patch.object(DraftSetupManager, "save_to_digitalocean_spaces", AsyncMock(return_value=spaces_key)),
        patch.object(DraftSetupManager, "get_pack_first_picks", MagicMock(return_value=["new"])),
        patch("services.draft_setup_manager.drop_uncollected", AsyncMock(return_value=list(dropped))),
        patch("services.draft_setup_manager.loan_changed", MagicMock()),
    )


@pytest.mark.asyncio
async def test_capture_replaces_the_log_of_a_restarted_draft():
    m = _manager()
    ds = _captured(_draft_data())
    db_factory, session = _mock_db_session(ds)
    gone = SimpleNamespace(id=7)
    spaces, picks, drop, panel = _supersede_patches("team/second.json", dropped=[gone])
    with patch("services.draft_setup_manager.db_session", db_factory), spaces, picks, drop, panel:
        ok = await m.capture_draft_log(_second_run())

    assert ok is True
    assert ds.draft_data == _second_run()
    assert ds.spaces_object_key == "team/second.json"
    assert ds.logs_captured_at > datetime(2026, 11, 5, 22, 8)
    assert ds.unlock_at > ds.logs_captured_at
    assert ds.pack_first_picks == {"d1": ["new"], "d2": ["new"]}
    assert ds.team_logs_posted_at is None, "the pools posted from the first log are stale"
    assert ds.drafttable_url is None, "the table page built from the first log is stale"
    drop.new.assert_awaited_once_with(session, "draft:sid")
    panel.new.assert_called_once_with(gone)
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_a_superseding_capture_whose_upload_fails_leaves_the_row_uncaptured():
    """The reconciler retries a capture by its NULL stamp. A supersede that
    kept the first run's stamp after a failed upload would never be retried:
    the new log is in the row, so the next attempt would read as the same run
    and be skipped, with the first run's Spaces files, pools and first picks
    left standing behind it."""
    m = _manager()
    ds = _captured(_draft_data())
    db_factory, session = _mock_db_session(ds)
    spaces, picks, drop, panel = _supersede_patches(None)
    with patch("services.draft_setup_manager.db_session", db_factory), spaces, picks, drop, panel:
        ok = await m.capture_draft_log(_second_run())

    assert ok is False
    assert ds.draft_data == _second_run()
    assert ds.logs_captured_at is None
    assert ds.spaces_object_key is None
    assert ds.pack_first_picks is None
    assert ds.unlock_at is None
    assert ds.team_logs_posted_at is None
    assert ds.drafttable_url is None
    drop.new.assert_awaited_once_with(session, "draft:sid")
    session.commit.assert_awaited()


@pytest.mark.asyncio
async def test_capture_keeps_a_published_log_even_when_a_later_one_arrives():
    m = _manager()
    ds = _captured(_draft_data(), data_received=True)
    db_factory, _ = _mock_db_session(ds)
    spaces, picks, drop, panel = _supersede_patches("team/second.json")
    with patch("services.draft_setup_manager.db_session", db_factory), spaces, picks, drop, panel:
        ok = await m.capture_draft_log(_second_run())

    assert ok is True
    spaces.new.assert_not_called()
    assert ds.draft_data == _draft_data()
    assert ds.team_logs_posted_at is not None
    drop.new.assert_not_called()


@pytest.mark.asyncio
async def test_capture_ignores_the_same_run_s_log_sent_again():
    m = _manager()
    ds = _captured(_draft_data())
    db_factory, _ = _mock_db_session(ds)
    spaces, picks, drop, panel = _supersede_patches("team/second.json")
    with patch("services.draft_setup_manager.db_session", db_factory), spaces, picks, drop, panel:
        ok = await m.capture_draft_log(_draft_data())

    assert ok is True
    spaces.new.assert_not_called()
    assert ds.team_logs_posted_at is not None
    drop.new.assert_not_called()


@pytest.mark.asyncio
async def test_start_draft_forgets_the_previous_run_s_log():
    """A start is the run boundary: nothing in memory from the old run is this
    draft's. The release-logs vote shares current_draft_log over the socket, and
    the end-of-draft handler waits for a log only when none is held."""
    m = _manager()
    m.current_draft_log = _draft_data()
    m.drafting = False
    m.draft_finished = True

    async def emit(event, callback=None, **_):
        callback({})
        return True

    m.socket_client = SimpleNamespace(emit=AsyncMock(side_effect=emit))
    await m.start_draft()

    assert m.drafting is True
    assert m.current_draft_log is None
