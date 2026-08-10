"""The discord-test harness guardrail hooks' patterns (.claude/hooks/).

The hooks are self-contained scripts, so these tests load them directly by
path (same convention as the frozen-migration tests) and pin the regexes'
allow/deny behavior — most importantly the quoted-redirect case, which was
a real bypass: `> "$CLAUDE_PROJECT_DIR/.env"` is the idiomatic form an
agent reaches for first, and the redirect branch originally required the
path to start unquoted.
"""
import importlib.util
from pathlib import Path

HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, HOOKS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


env_guard = _load("protect_env_file")
invites = _load("deny_discord_invites")
guild_confine = _load("confine_to_test_guild")


# ---- protect_env_file ------------------------------------------------------

DENIED_COMMANDS = [
    "echo x > .env",
    "echo x >> .env",
    'echo x > ".env"',
    "echo x > '.env'",
    'echo x > "$CLAUDE_PROJECT_DIR/.env"',   # the closed bypass
    'echo x >> "$HOME/.env"',
    "echo x > $HOME/.env",
    "echo x > .env.local",
    "echo x > .env-test",
    "sed -i 's/a/b/' .env",
    "tee .env < input",
    "tee -a .env",
    "rm .env",
    "rm -f /home/dev/DraftBot/.env",
    "truncate -s 0 .env",
    "cp other.env .env",
    'mv backup.env ".env"',
    "bash -c 'echo x > .env'",
]

ALLOWED_COMMANDS = [
    "grep TEST_GUILD_ID .env",              # reads stay allowed
    "cat .env",
    "grep -c BOT_TOKEN .env && echo present",
    "echo x > .environment_notes.txt",      # not a dotenv-family name
    "python -m pytest tests/",
    "cp .env.example docs/example.txt",     # .env as SOURCE, not dest
]


def test_env_write_commands_denied():
    for cmd in DENIED_COMMANDS:
        assert env_guard.WRITE_PATTERNS.search(cmd), f"should deny: {cmd}"


def test_env_reads_and_unrelated_commands_allowed():
    for cmd in ALLOWED_COMMANDS:
        assert not env_guard.WRITE_PATTERNS.search(cmd), f"should allow: {cmd}"


# ---- deny_discord_invites --------------------------------------------------

def test_invite_urls_denied():
    for url in [
        "https://discord.gg/abc123",
        "https://discord.com/invite/abc123",
        "https://discordapp.com/invite/abc123",
        "https://discord.new/templatecode",
        "https://ptb.discord.com/invite/abc",
    ]:
        assert invites.BLOCKED.search(url), f"should deny: {url}"


def test_non_invite_urls_allowed():
    for url in [
        "https://discord.com/login",
        "https://discord.com/channels/123/456",
        "https://example.com/discord.gg-writeup",
    ]:
        assert not invites.BLOCKED.search(url), f"should allow: {url}"


# ---- confine_to_test_guild -------------------------------------------------

def test_channel_url_guild_capture():
    m = guild_confine.CHANNELS.search("https://discord.com/channels/111/222")
    assert m and m.group(1) == "111"
    m = guild_confine.CHANNELS.search("https://ptb.discord.com/channels/@me")
    assert m and m.group(1) == "@me"       # DMs captured -> hook denies them
    # Query/fragment can't smuggle a guild into the capture.
    m = guild_confine.CHANNELS.search("https://discord.com/channels/111?x=999#999")
    assert m and m.group(1) == "111"


def test_non_channel_urls_get_no_opinion():
    for url in ["https://discord.com/login", "https://example.com/channels/999"]:
        assert not guild_confine.CHANNELS.search(url), f"no opinion expected: {url}"
