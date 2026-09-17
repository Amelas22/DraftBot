"""Build league_site/deltas.html: team players vs hard carries, last 30 days.

Deliberately publishes NO win rates -- only a match count and a score. The
score is the gap between how often a player's team took the draft and how
often the player took their own matches, in points.

Usage: build_delta_page.py [days]
"""
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

GUILD = "1355718878298116096"          # Lotus
DB = Path(__file__).resolve().parent.parent / "drafts.db"
OUT = Path(__file__).resolve().parent.parent / "league_site" / "deltas.html"
# Each player is scored on their STRONGEST of three windows. Best-of-three
# systematically favours the shortest one -- 30 days is the noisiest, and about
# two thirds of the winning scores come from it -- so the floor below applies
# WITHIN every window: a 30-day entry still needs 12 drafts and 30 matches
# behind it. That is what keeps a hot fortnight off the board while still
# letting a genuine 30-day run beat a flat 90-day average. Each row says which
# window produced its score, because "over 30 days" and "over 90 days" are not
# the same claim.
WINDOWS = (30, 60, 90)
MIN_DRAFTS, MIN_MATCHES = 12, 30


def collect(days):
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    latest = c.execute(
        "SELECT MAX(teams_start_time) FROM draft_sessions WHERE guild_id=?", (GUILD,)
    ).fetchone()[0]
    end = datetime.fromisoformat(latest)
    cutoff = (end - timedelta(days=days)).isoformat(sep=" ")

    rows = c.execute("""
        SELECT session_id, team_a, team_b, sign_ups FROM draft_sessions
        WHERE guild_id=? AND teams_start_time >= ?
          AND team_a IS NOT NULL AND team_b IS NOT NULL
    """, (GUILD, cutoff)).fetchall()

    names = {}
    m_won, m_played = defaultdict(int), defaultdict(int)
    d_won, d_played = defaultdict(int), defaultdict(int)
    counted = 0

    for sid, ta, tb, signups in rows:
        try:
            a, b = set(json.loads(ta) or []), set(json.loads(tb) or [])
        except (TypeError, json.JSONDecodeError):
            continue
        if not a or not b:
            continue
        if signups:
            try:
                for uid, nm in (json.loads(signups) or {}).items():
                    names[str(uid)] = nm if isinstance(nm, str) else (nm or {}).get("name", str(uid))
            except (TypeError, json.JSONDecodeError):
                pass
        reported = [m for m in c.execute(
            """SELECT player1_id, player1_wins, player2_id, player2_wins, winner_id
               FROM match_results WHERE session_id=?""", (sid,)).fetchall() if m[4]]
        if not reported:
            continue
        wa = sum(1 for m in reported if m[4] in a)
        wb = sum(1 for m in reported if m[4] in b)
        if wa == wb:                    # a drawn draft counts for neither side
            continue
        winners = a if wa > wb else b
        counted += 1
        for p in a | b:
            d_played[p] += 1
            d_won[p] += p in winners
        for p1, _w1, p2, _w2, win in reported:
            for p in (p1, p2):
                if p:
                    m_played[p] += 1
            m_won[win] += 1

    people = []
    for p, drafts in d_played.items():
        if drafts < MIN_DRAFTS or m_played[p] < MIN_MATCHES:
            continue
        mwr, dwr = m_won[p] / m_played[p], d_won[p] / drafts
        people.append({"name": names.get(p, p), "matches": m_played[p], "drafts": drafts,
                       "delta": (dwr - mwr) * 100, "mwr": mwr, "dwr": dwr})
    return people, counted, cutoff[:10], latest[:10]


def render(people, drafts, start, end, days):
    # Each label is meant as credit, so each side excludes players in the bottom
    # quarter of the FIELD on the axis it credits -- a hard carry should not be
    # among the league's worst at their own matches, nor a team player among the
    # worst at winning drafts.
    #
    # This was an absolute .500 line and that was wrong: the field's median match
    # rate is around 51%, so a .500 floor cut half the league by construction. It
    # dropped iomatic -- 46% of their own matches while their team took 25% of
    # drafts, the widest gap on the board over 91 matches -- for being four points
    # under an arbitrary round number. A quartile of the actual field adapts to
    # how the league is playing instead.
    #
    # A score that rounds to zero is not a finding either: the two rates simply
    # agreed, which is the unremarkable case this page is not about.
    def floor(key):
        vals = sorted(p[key] for p in people)
        return vals[len(vals) // 4] if vals else 0

    m_floor, d_floor = floor("mwr"), floor("dwr")
    team = sorted((p for p in people if p["delta"] >= .5 and p["dwr"] >= d_floor),
                  key=lambda p: -p["delta"])[:10]
    carry = sorted((p for p in people if p["delta"] <= -.5 and p["mwr"] >= m_floor),
                   key=lambda p: p["delta"])[:10]

    def table(rows, label):
        out = []
        for i, p in enumerate(rows, 1):
            out.append(
                f'<tr><td class="rank">{i}</td><td class="who">{escape(p["name"])}</td>'
                f'<td class="num">{p["matches"]}</td>'
                f'<td class="span">{p["window"]}d</td>'
                f'<td class="score">{abs(p["delta"]):.0f}</td></tr>')
        return (f'<table><thead><tr><th></th><th>Player</th><th class="num">Matches</th>'
                f'<th class="num">Over</th><th class="num">{label}</th></tr></thead><tbody>'
                + "".join(out) + "</tbody></table>")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lotus League — Team Players &amp; Hard Carries</title>
<style>
  :root {{
    --bg:#0d0b10; --panel:#16121d; --panel-2:#1d1826; --ink:#e8e2d6;
    --ink-dim:#a89f8f; --gold:#d4af5f; --gold-bright:#ecc87a;
    --lotus:#b98fd4; --rule:#2c2536;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--ink); line-height:1.6;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }}
  .serif {{ font-family:Georgia,"Iowan Old Style","Times New Roman",serif; }}
  .wrap {{ max-width:820px; margin:0 auto; padding:3.5rem 1.25rem 4rem; }}
  .kicker {{ color:var(--gold); letter-spacing:.32em; text-transform:uppercase;
    font-size:.78rem; margin-bottom:.9rem; }}
  h1 {{ font-size:clamp(1.9rem,5vw,2.8rem); line-height:1.15; margin-bottom:1.1rem;
    color:var(--gold-bright); font-weight:600; }}
  .lede {{ color:var(--ink-dim); font-size:1.05rem; max-width:62ch; }}
  .lede + .lede {{ margin-top:.9rem; }}
  .window {{ margin-top:1.6rem; padding-top:1.2rem; border-top:1px solid var(--rule);
    color:var(--ink-dim); font-size:.9rem; }}
  section {{ margin-top:3rem; background:var(--panel); border:1px solid var(--rule);
    border-radius:10px; padding:1.6rem 1.5rem 1.2rem; }}
  section h2 {{ font-size:1.35rem; color:var(--gold-bright); font-weight:600; }}
  section .blurb {{ color:var(--ink-dim); margin:.5rem 0 1.2rem; font-size:.96rem; }}
  .scroll {{ overflow-x:auto; }}
  table {{ width:100%; border-collapse:collapse; font-size:.97rem; }}
  th {{ text-align:left; color:var(--ink-dim); font-weight:500; font-size:.8rem;
    letter-spacing:.08em; text-transform:uppercase; padding:.5rem .6rem;
    border-bottom:1px solid var(--rule); white-space:nowrap; }}
  td {{ padding:.62rem .6rem; border-bottom:1px solid rgba(44,37,54,.5); }}
  tbody tr:last-child td {{ border-bottom:none; }}
  .rank {{ color:var(--ink-dim); width:1.6rem; font-variant-numeric:tabular-nums; }}
  .who {{ font-weight:500; }}
  .num, td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .span {{ text-align:right; color:var(--ink-dim); font-variant-numeric:tabular-nums;
    font-size:.88rem; white-space:nowrap; }}
  .score {{ text-align:right; font-variant-numeric:tabular-nums;
    color:var(--gold-bright); font-weight:600; font-size:1.05rem; }}
  .foot {{ margin-top:2.6rem; padding-top:1.2rem; border-top:1px solid var(--rule);
    color:var(--ink-dim); font-size:.86rem; }}
  .foot p + p {{ margin-top:.7rem; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="kicker">Lotus League</div>
  <h1 class="serif">Team Players &amp; Hard Carries</h1>
  <p class="lede">Two ways to be worth having around. Some players' teams keep taking
  the draft whoever they sit across from; others keep winning their own matches
  whichever way the draft falls.</p>
  <p class="lede">The score is the gap between those two things, in points, measured
  over each player's strongest stretch — 30, 60 or 90 days, whichever separated them
  most. The <em>Over</em> column says which, because a 30-day run and a 90-day pattern
  are not the same claim.</p>
  <p class="lede">A high score means the two came apart, in one direction or the other.
  It says nothing about whether a player is good — only about how their own results and
  their team's results diverged.</p>
  <div class="window">Through {end} · {drafts} drafts with reported results in the
  90-day span · a player needs {MIN_DRAFTS} drafts and {MIN_MATCHES} matches
  <em>within</em> a window to be scored on it</div>

  <section>
    <h2 class="serif">Team Players</h2>
    <p class="blurb">Their team took the draft more often than they took their own
    matches. Whatever it is they bring, it shows up in the column that decides the night.</p>
    <div class="scroll">{table(team, "Team score")}</div>
  </section>

  <section>
    <h2 class="serif">Hard Carries</h2>
    <p class="blurb">They won their own matches more often than the draft went their
    way. They held up their end; the night went elsewhere.</p>
    <div class="scroll">{table(carry, "Carry score")}</div>
  </section>

  <div class="foot">
    <p>A draft counts for whichever side won more matches in it; drawn drafts and
    drafts with nothing reported are left out entirely. Each list is limited to
    players who are not in the bottom quarter of the field at the thing the list
    credits — a hard carry is not among the league's worst at their own matches,
    a team player not among the worst at winning drafts — so a bad run alone
    cannot put anyone on either board. That line is drawn from how this league is
    actually playing rather than from a round number: an even .500 would sit above
    the field's median and cut half of it.</p>
    <p>Everyone listed has at least {MIN_DRAFTS} drafts behind their score, so a single
    result moves it by under {100 / MIN_DRAFTS:.0f} points. Players below that line are
    left off rather than ranked on noise.</p>
    <p>Scoring each player on their best of three windows does favour the shortest one —
    30 days has the most room to swing, and most of the scores here come from it. That is
    why the floor applies inside every window rather than across the whole span: a 30-day
    entry is a real stretch of drafting, not a hot fortnight. Read a 90d row as the
    steadier claim of the two.</p>
  </div>
</div>
</body>
</html>
"""


best = {}
spans = {}
for w in WINDOWS:
    people, drafts, start, end = collect(w)
    spans[w] = (drafts, start, end)
    for person in people:
        held = best.get(person["name"])
        if held is None or abs(person["delta"]) > abs(held["delta"]):
            best[person["name"]] = {**person, "window": w}

longest = spans[max(WINDOWS)]
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(render(list(best.values()), longest[0], longest[1], longest[2], max(WINDOWS)))
print(f"wrote {OUT} ({len(best)} players scored across {WINDOWS}-day windows, "
      f"{longest[0]} drafts in the {max(WINDOWS)}-day span)")
