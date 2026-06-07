"""TDD tests for two missing-import bugs caught by static analysis (pylint E0602).

Each bug is a name referenced in code but never imported, so the runtime path
crashes with NameError on first invocation. Tests verify the symbol is reachable
from its module's namespace AND exercise the actual code path to confirm no
NameError fires.

Bugs covered:
  1. views.py uses `split_content_for_embed` (defined in utils.py:69) at
     views.py:2351, inside update_draft_message's >1000-char sign-ups branch.
  2. cogs/debt_commands.py uses `DebtLedger` (defined in models/debt_ledger.py)
     at debt_commands.py:208-214, in the /debts_history slash command.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestSplitContentForEmbedImport:
    def test_views_module_exposes_split_content_for_embed(self):
        import views
        assert hasattr(views, "split_content_for_embed"), (
            "views.py calls split_content_for_embed() but never imports it. "
            "Add it to the `from utils import (...)` block."
        )

    @pytest.mark.asyncio
    async def test_update_draft_message_handles_long_signup_list_without_nameerror(self):
        """Trigger the >1000-char branch and confirm split_content_for_embed resolves."""
        import views

        # 40 players with long names → sign_ups_str will easily exceed 1000 chars
        sign_ups = {
            str(1_000_000 + i): f"PlayerWithAFairlyLongDisplayName_{i:03d}"
            for i in range(40)
        }
        draft_session = MagicMock()
        draft_session.sign_ups = sign_ups
        draft_session.draft_channel_id = "111111"
        draft_session.message_id = "222222"
        draft_session.cube = "LSVCube"
        draft_session.session_id = "test_session"
        draft_session.session_type = "random"

        embed = MagicMock()
        embed.fields = []
        message = MagicMock()
        message.embeds = [embed]
        channel = MagicMock()
        channel.guild = MagicMock()
        channel.fetch_message = AsyncMock(return_value=message)
        bot = MagicMock()
        bot.get_channel = MagicMock(return_value=channel)

        # Sanity: confirm our setup will trigger the >1000-char branch
        expected_str = f"**Players ({len(sign_ups)}):**\n" + "\n".join(sign_ups.values())
        assert len(expected_str) > 1000

        # update_draft_message has a broad `except Exception` that swallows the
        # NameError silently, so we can't catch it directly. Instead, assert that
        # the function reached the very end (message.edit) — which it can't if
        # NameError fires at line 2351.
        message.edit = AsyncMock()
        with patch.object(views, "get_draft_session", AsyncMock(return_value=draft_session)), \
             patch.object(views, "get_display_name_by_id", side_effect=lambda uid, guild, stored: stored), \
             patch.object(views, "get_cube_thumbnail_url", return_value="https://example.com/thumb.png"):
            await views.update_draft_message(bot, "test_session")

        message.edit.assert_called_once()


class TestDebtLedgerImport:
    def test_debt_commands_module_exposes_debt_ledger(self):
        import cogs.debt_commands as dc
        assert hasattr(dc, "DebtLedger"), (
            "cogs/debt_commands.py uses DebtLedger but never imports it. "
            "Add `from models.debt_ledger import DebtLedger`."
        )

    @pytest.mark.asyncio
    async def test_debts_history_callback_does_not_raise_nameerror(self):
        """Call the /debts_history callback with mocks and confirm it reaches the
        end without hitting NameError on DebtLedger."""
        from cogs.debt_commands import DebtCommands

        ctx = MagicMock()
        ctx.author.id = 111
        ctx.guild.id = 222
        ctx.defer = AsyncMock()
        ctx.followup.send = AsyncMock()

        player = MagicMock()
        player.id = 333
        player.display_name = "OtherPlayer"

        mock_session = MagicMock()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_session.execute = AsyncMock(return_value=mock_result)
        db_session_cm = MagicMock()
        db_session_cm.__aenter__ = AsyncMock(return_value=mock_session)
        db_session_cm.__aexit__ = AsyncMock(return_value=None)

        cog = DebtCommands(MagicMock())
        with patch("cogs.debt_commands.db_session", MagicMock(return_value=db_session_cm)):
            # Invoke the underlying callback directly (bypassing the slash-command wrapper).
            await cog.debts_history.callback(cog, ctx, player)
