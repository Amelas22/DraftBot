"""features.wallet opens the tix wallet without making /draft a staked draft."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import config
from helpers import money_gate
from services.donation_day import draft_kind


@contextmanager
def _features(**flags):
    with patch.object(config, "get_config", return_value={"features": flags}):
        yield


def _ctx():
    return SimpleNamespace(guild=SimpleNamespace(id=1))


def test_wallet_flag_alone_enables_the_wallet():
    with _features(wallet=True):
        assert config.wallet_enabled("g") is True
        assert config.is_money_server("g") is False


def test_money_server_implies_the_wallet():
    with _features(money_server=True):
        assert config.wallet_enabled("g") is True


def test_neither_flag_means_no_wallet():
    with _features():
        assert config.wallet_enabled("g") is False


def test_gate_read_opens_on_a_wallet_only_server():
    with _features(wallet=True):
        assert money_gate.gate_read(_ctx()) is None


def test_gate_read_refusal_does_not_mention_money_servers():
    with _features():
        assert money_gate.gate_read(_ctx()) == "The tix wallet isn't enabled on this server."


def test_howto_shown_on_a_wallet_only_server():
    with _features(wallet=True):
        assert "/wallet deposit" in money_gate.wallet_howto("g")


def test_draft_is_not_staked_on_a_wallet_only_server():
    with _features(wallet=True):
        kind = draft_kind(money_server=config.is_money_server("g"), event_active=False)
    assert kind is None


def test_default_config_has_the_flag_off():
    cfg = config.Config()
    assert cfg.default_config["features"]["wallet"] is False
    assert cfg.special_guild_config["features"]["wallet"] is False


def test_gate_serve_follows_the_wallet_flag_not_the_money_server_flag():
    with _features(wallet=True), \
            patch("helpers.money_gate.get_client", return_value=SimpleNamespace(enabled=True)):
        assert money_gate.gate_serve(_ctx()) is None


def test_gate_serve_refuses_a_server_with_no_wallet():
    with _features(), \
            patch("helpers.money_gate.get_client", return_value=SimpleNamespace(enabled=True)):
        assert money_gate.gate_serve(_ctx()) == "The tix wallet isn't enabled on this server."


def test_gate_serve_reports_an_unconfigured_tradebot_on_a_wallet_server():
    with _features(wallet=True), \
            patch("helpers.money_gate.get_client", return_value=SimpleNamespace(enabled=False)):
        assert "isn't configured" in money_gate.gate_serve(_ctx())
