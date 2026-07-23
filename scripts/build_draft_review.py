"""Build a self-contained HTML draft-review page from the Chrome extension's own
viewer assets, with the log and Read the Table data pre-embedded. Saves the log
and HTML locally, then uploads and presigns."""
import asyncio
import json
import os
import sqlite3

from dotenv import load_dotenv

load_dotenv(os.path.join(os.getcwd(), ".env"))

from helpers.digital_ocean_helper import DigitalOceanHelper
from helpers.magicprotools_helper import MagicProtoolsHelper

KEY = "team/PowerLSV-1784686408228-DBUNQVWXO0.json"
EXT = "/home/rothenell/mtg/draftmancer-wheel-extension"
OUT = "/tmp/claude-1000/-home-rothenell-mtg-DraftBot/8b656e8c-720b-45a4-bb3e-a5aace655c47/scratchpad"

# Same order as viewer.html.
JS_FILES = [
    "src/wheel-core.js",
    "src/viewer/log-parser.js",
    "src/viewer/scryfall.js",
    "src/viewer/replay.js",
    "src/viewer/deck-layout.js",
    "src/viewer/prefs.js",
    "src/history.js",
    "src/viewer/deck-stats.js",
    "src/viewer/mana-sources.js",
    "src/viewer/mana-report.js",
    "src/viewer/table-read.js",
    "src/viewer/viewer.js",
]


def _read(rel):
    with open(os.path.join(EXT, rel), encoding="utf-8") as f:
        return f.read()


def _card_image(card):
    """Front-face image URL from Draftmancer carddata. `image_uris` is keyed by
    language (en, zhs, …), not size; prefer English, else any language, else "".
    (Also tolerates Scryfall-shaped {normal: …} data.)"""
    imgs = card.get("image_uris") or {}
    if not isinstance(imgs, dict):
        return ""
    return imgs.get("en") or imgs.get("normal") or next(iter(imgs.values()), "") or ""


def assert_js_parses(js, label="viewer bundle"):
    """Fail loudly if the concatenated viewer JS isn't parseable as one script.

    The hosted page inlines every viewer module into a SINGLE <script>, so a
    top-level name collision between two modules is a redeclaration SyntaxError
    that silently breaks the whole page (no JS runs, the replay never loads).
    node's own test runner can't catch this — it loads each module separately —
    so we parse-check the actual concatenation here before shipping.
    """
    import subprocess
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(js)
        result = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        if result.returncode != 0:
            raise SystemExit(
                f"{label} does not parse — likely a top-level name collision "
                f"between viewer modules:\n{result.stderr.strip()}"
            )
    finally:
        os.unlink(path)


def build_table_data(draft_data, viewer_user_id):
    users = draft_data.get("users", {})
    carddata = draft_data.get("carddata", {})
    seats = []
    for uid, u in users.items():
        if uid == viewer_user_id:
            continue
        picks = []
        for pk in u.get("picks", []):
            for i in (pk.get("pick") or []):
                c = carddata.get(pk["booster"][i], {})
                picks.append({
                    "pack": pk["packNum"], "pick": pk["pickNum"],
                    "name": c.get("name"), "colors": c.get("colors") or [],
                    "rating": c.get("rating"), "cmc": c.get("cmc"),
                    "type": c.get("type") or "",
                    "img": _card_image(c),
                })
        seats.append({"name": u.get("userName"), "picks": picks})
    return {"seats": seats}


def build_seat_ring(draft_data, viewer_user_id):
    """Seat names starting at the viewer, in Pack-1 pass direction, derived from
    pick adjacency (consecutive pickers of the same booster card in pack 0 are
    seat-neighbors). Returns None if a clean N-cycle can't be walked."""
    from collections import defaultdict, Counter
    users = draft_data.get("users", {})
    name = {uid: u.get("userName") for uid, u in users.items()}
    seen = defaultdict(list)  # cardid -> [(pickNum, uid)]
    for uid, u in users.items():
        for pk in u.get("picks", []):
            if pk.get("packNum") != 0:
                continue
            for cid in pk.get("booster", []):
                seen[cid].append((pk.get("pickNum"), uid))
    votes = defaultdict(Counter)  # uid -> Counter(next uid)
    for lst in seen.values():
        lst.sort(key=lambda t: t[0])
        for (p1, u1), (p2, u2) in zip(lst, lst[1:]):
            if p2 == p1 + 1 and u1 != u2:
                votes[u1][u2] += 1
    n = len(users)
    if n < 2:
        return None
    ring, cur, used = [viewer_user_id], viewer_user_id, {viewer_user_id}
    for _ in range(n - 1):
        if not votes.get(cur):
            return None
        nxt = votes[cur].most_common(1)[0][0]
        if nxt in used:
            return None
        ring.append(nxt); used.add(nxt); cur = nxt
    return [name[uid] for uid in ring]


async def main():
    dd = await DigitalOceanHelper().download_json(KEY)
    if not dd:
        con = sqlite3.connect("drafts.db")
        row = con.execute(
            "SELECT draft_data FROM draft_sessions WHERE spaces_object_key = ?", (KEY,)
        ).fetchone()
        dd = json.loads(row[0]) if row and row[0] else None
        con.close()
    if not dd:
        raise SystemExit("no draft data")

    users = dd.get("users", {})
    aber = next((uid for uid, u in users.items() if str(u.get("userName", "")).lower() == "aber"), None)
    if not aber:
        raise SystemExit("aber not found in draft")
    log = MagicProtoolsHelper().convert_to_magicprotools_format(dd, aber)
    table = build_table_data(dd, aber)
    table["ring"] = build_seat_ring(dd, aber)

    with open(os.path.join(OUT, "aber_log.txt"), "w", encoding="utf-8") as f:
        f.write(log)

    css = _read("src/viewer/viewer.css")
    combined_js = "\n\n".join(f"// ===== {p} =====\n{_read(p)}" for p in JS_FILES)
    assert_js_parses(combined_js)  # never ship a bundle that won't run

    html = _read("viewer.html")
    html = html.replace(
        '<link rel="stylesheet" href="src/viewer/viewer.css" />',
        f"<style>\n{css}\n</style>",
    )
    html = html.replace(
        "<title>Draftmancer Draft Log Replay</title>",
        "<title>aber — PowerLSV draft (2026-07-21) · Replay</title>",
    )
    # Everything up to the first <script src> is the full page markup (landing + replay).
    idx = html.index('<script src="src/wheel-core.js">')
    # back up to the start of that line so we drop the whole script block
    idx = html.rfind("\n", 0, idx) + 1
    prefix = html[:idx]

    boot = (
        "const DMW_LOG = " + json.dumps(log) + ";\n"
        "window.DMW_TABLE = " + json.dumps(table) + ";\n"
        "window.addEventListener('load', function () {\n"
        "  var p = document.getElementById('dmw-paste');\n"
        "  var b = document.getElementById('dmw-load');\n"
        "  if (p && b) { p.value = DMW_LOG; b.click(); }\n"
        "});\n"
    )

    final = (
        prefix
        + "<script>\n" + combined_js + "\n</script>\n"
        + "<script>\n" + boot + "</script>\n"
        + "  </body>\n</html>\n"
    )

    out_html = os.path.join(OUT, "aber_replay.html")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(final)

    print(f"players: {[u.get('userName') for u in users.values()]}")
    print(f"aber id: {aber}")
    print(f"log: {len(log)} chars; html: {len(final)} bytes")
    print(f"wrote {out_html}")


if __name__ == "__main__":
    asyncio.run(main())
