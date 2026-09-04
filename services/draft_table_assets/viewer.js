/* Multi-party draft viewer.
 *
 * One clock drives everything. At (pack, pick) each seat is holding exactly one
 * booster, so the six seat rows are just a lookup into the reconstructed
 * boosters. Pinning a booster follows it round the ring; clicking a card asks
 * the fate index where that card ended up.
 */
(function () {
  "use strict";

  var D = window.DRAFT;
  var SEATS = D.ring.length;
  var PACKS = D.directions.length;
  var PICKS = D.boosters[0].steps.length;

  /* Three camera distances, ordered by how far back you stand. Information
     widens as you pull back: first person knows only what its seat has seen,
     over-the-shoulder adds the two neighbours' current packs, and overhead is
     omniscient -- every seat, and the future of any pack you pin. */
  var MODES = [
    { id: "first",    label: "First person",       hint: "one seat, and only what they could see" },
    { id: "shoulder", label: "Over the shoulder",  hint: "the seat that feeds them, them, and the seat they feed" },
    { id: "overhead", label: "Overhead",           hint: "the whole table, past and future" }
  ];

  var state = { pack: 1, pick: 1, pinned: null, card: null, query: "",
              open: {}, poolSort: "draft", mode: "overhead", focus: D.ring[0],
              hidePicks: false, revealed: false };

  /* Are picks concealed right this moment? Hiding is a standing preference;
     revealing is per-pick and is dropped again the moment the clock moves, so
     the guess-then-check loop survives stepping through a whole pack. */
  function hidingNow() { return state.hidePicks && !state.revealed; }

  /* Only the overhead camera may read the future. Pinning a booster to see who
     takes what later, and a card's full fate, both live behind this. */
  function knowsFuture() { return state.mode === "overhead"; }

  // (pack, pick, seat) -> the step that seat is playing then. Built once: every
  // render is a constant-time lookup rather than a scan over 18 boosters.
  var stepAt = {};
  D.boosters.forEach(function (b) {
    b.steps.forEach(function (s) {
      stepAt[s.pack + "|" + s.pick + "|" + s.seat] = { booster: b, step: s };
    });
  });

  var eventPicks = {};
  D.events.forEach(function (e) { eventPicks[e.pack + "|" + e.pick] = e; });

  function card(id) { return D.cards[id] || { name: id, colors: [], small: "", normal: "" }; }

  var COLOURS = [["W", "White"], ["U", "Blue"], ["B", "Black"],
                 ["R", "Red"], ["G", "Green"], ["C", "Colourless"]];

  /* Everything `seat` is holding once (state.pack, state.pick) has been played.
     The payload lists each seat's picks in draft order with the step a card
     left again, so this is a walk, not a search. */
  function poolThrough(seat) {
    var out = [];
    var entries = D.pools[seat] || [];
    // With picks hidden the pool stops one pick short: a card appearing in the
    // pool is exactly the answer the hidden pick is withholding.
    var lastPick = state.pick - (hidingNow() ? 1 : 0);
    for (var i = 0; i < entries.length; i++) {
      var e = entries[i];
      if (e.pack > state.pack || (e.pack === state.pack && e.pick > lastPick)) break;
      if (e.gone && (e.gone[0] < state.pack ||
          (e.gone[0] === state.pack && e.gone[1] <= state.pick))) continue;
      out.push(e);
    }
    return out;
  }

  function colourTally(entries) {
    var counts = {};
    entries.forEach(function (e) {
      var cols = card(e.card).colors;
      if (!cols.length) { counts.C = (counts.C || 0) + 1; return; }
      cols.forEach(function (c) { counts[c] = (counts[c] || 0) + 1; });
    });
    return counts;
  }
  /* Who hands this seat its boosters, and who it hands them to. Verified
     against the log: with directions[pack-1] === 1 a seat is fed by ring[i-1]
     and feeds ring[i+1]; at -1 the two swap. Pack 2 reverses, so the player
     reading signals from their left in pack 1 reads their right in pack 2 --
     which is exactly why these are labelled by role and not by side. */
  function neighbours(seat, pack) {
    var i = D.ring.indexOf(seat);
    var d = D.directions[pack - 1] === 1 ? 1 : -1;
    var wrap = function (n) { return D.ring[((n % SEATS) + SEATS) % SEATS]; };
    return { upstream: wrap(i - d), downstream: wrap(i + d) };
  }

  /* Every card `seat` has actually laid eyes on: the contents of every booster
     that has sat in front of them at or before now. First person is allowed to
     know about these and nothing else. */
  function seenBy(seat) {
    var seen = {};
    for (var p = 1; p <= state.pack; p++) {
      var last = p === state.pack ? state.pick : PICKS;
      for (var k = 1; k <= last; k++) {
        var f = stepAt[p + "|" + k + "|" + seat];
        if (!f) continue;
        f.step.contents.forEach(function (id) { seen[id] = true; });
      }
    }
    return seen;
  }

  /* This seat's own relationship with a card: every time it passed in front of
     them, and whether they took it. A player legitimately knows a card wheeled
     once it comes back -- that is memory, not foresight. */
  function ownHistory(seat, cardId) {
    var out = [];
    for (var p = 1; p <= state.pack; p++) {
      // Same reason as poolThrough: while the pick is hidden, "you took it"
      // must not be one click away.
      var last = p === state.pack ? state.pick - (hidingNow() ? 1 : 0) : PICKS;
      for (var k = 1; k <= last; k++) {
        var f = stepAt[p + "|" + k + "|" + seat];
        if (!f || f.step.contents.indexOf(cardId) === -1) continue;
        out.push({ pack: p, pick: k, took: f.step.taken.indexOf(cardId) !== -1 });
      }
    }
    return out;
  }

  function teamOf(seat) { return D.meta.teams[seat] || "A"; }
  function teamColor(seat) { return teamOf(seat) === "A" ? "var(--team-a)" : "var(--team-b)"; }

  /* Captions sit under a 62px thumbnail, so the seat needs a short form.
     "Birb // Entropy263" -> "Birb", "Strider / osmanozguney" -> "Strider". */
  var shortName = (function () {
    var cache = {};
    return function (seat) {
      if (cache[seat]) return cache[seat];
      var head = seat.split(/\s*\/\/?\s*/)[0].trim() || seat;
      cache[seat] = head.length > 11 ? head.slice(0, 10) + "\u2026" : head;
      return cache[seat];
    };
  })();

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  /* A card thumbnail. Scryfall art is the label; the name is kept as a text
     fallback so the page still reads if an image fails or the host is offline. */
  function cardEl(id, opts) {
    opts = opts || {};
    var c = card(id);
    var b = el("button", "card" + (opts.cls ? " " + opts.cls : ""));
    b.type = "button";
    if (c.small) {
      var img = document.createElement("img");
      img.src = c.small;
      img.alt = "";
      img.loading = "lazy";
      // Falling back to the printed name keeps the page usable offline or if
      // Scryfall drops a URL, rather than leaving a silent empty rectangle.
      img.addEventListener("error", function () { b.classList.add("noimg"); });
      b.appendChild(img);
    } else {
      b.classList.add("noimg");
    }
    b.title = c.name + (c.cost ? "  " + c.cost : "");
    b.setAttribute("aria-label", c.name);
    b.appendChild(el("span", "fallback", c.name));
    b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      state.card = c.name;
      render();
    });
    return b;
  }

  /* ---------------- clock ---------------- */

  function setStep(pack, pick) {
    var moved = pack !== state.pack || pick !== state.pick;
    state.pack = Math.min(Math.max(pack, 1), PACKS);
    state.pick = Math.min(Math.max(pick, 1), PICKS);
    if (moved) state.revealed = false;
    render();
  }

  function advance(delta) {
    var flat = (state.pack - 1) * PICKS + (state.pick - 1) + delta;
    flat = Math.min(Math.max(flat, 0), PACKS * PICKS - 1);
    setStep(Math.floor(flat / PICKS) + 1, (flat % PICKS) + 1);
  }

  function renderClock() {
    var wrap = el("div", "clock");
    var row = el("div", "clockrow");

    var packs = el("div", "packbtns");
    for (var p = 1; p <= PACKS; p++) {
      (function (n) {
        var b = el("button", "packbtn" + (n === state.pack ? " on" : ""), "Pack " + n);
        b.type = "button";
        b.addEventListener("click", function () { setStep(n, 1); });
        packs.appendChild(b);
      })(p);
    }
    row.appendChild(packs);

    var prev = el("button", "step", "◀ Prev");
    prev.type = "button";
    prev.disabled = state.pack === 1 && state.pick === 1;
    prev.addEventListener("click", function () { advance(-1); });
    row.appendChild(prev);

    var pos = el("div", "pos");
    pos.appendChild(el("span", null, "pick "));
    pos.appendChild(el("b", null, String(state.pick)));
    pos.appendChild(el("span", null, " / " + PICKS));
    row.appendChild(pos);

    var next = el("button", "step", "Next ▶");
    next.type = "button";
    next.disabled = state.pack === PACKS && state.pick === PICKS;
    next.addEventListener("click", function () { advance(1); });
    row.appendChild(next);

    var dir = D.directions[state.pack - 1] === 1 ? "passing left ↻" : "passing right ↺";
    row.appendChild(el("span", "dirnote", dir));

    var search = el("input", "search");
    search.type = "search";
    search.placeholder = "find a card…";
    search.value = state.query;
    search.addEventListener("input", function () {
      state.query = this.value;
      renderResults();
    });
    row.appendChild(search);
    wrap.appendChild(row);

    var scrub = el("div", "scrub");
    for (var i = 1; i <= PICKS; i++) {
      (function (n) {
        var t = el("button", "tick" +
          (n === state.pick ? " now" : n < state.pick ? " past" : "") +
          (eventPicks[state.pack + "|" + n] ? " event" : ""));
        t.type = "button";
        t.title = "pick " + n;
        t.setAttribute("aria-label", "go to pick " + n);
        t.addEventListener("click", function () { setStep(state.pack, n); });
        scrub.appendChild(t);
      })(i);
    }
    wrap.appendChild(scrub);
    return wrap;
  }

  /* ---------------- seats ---------------- */

  /* `seats` is a list of {seat, role}; role drives the visual hierarchy
     ("focus" is the player being watched, "side" the supporting neighbours).
     Overhead passes every seat with no role, which is the original layout. */
  function renderSeats(seats) {
    var wrap = el("div", "seats");
    seats.forEach(function (entry) {
      var seat = entry.seat, role = entry.role;
      var found = stepAt[state.pack + "|" + state.pick + "|" + seat];
      var row = el("div", "seat" + (role ? " role-" + role : ""));
      row.style.setProperty("--team", teamColor(seat));
      if (found && state.pinned === found.booster.index) row.classList.add("pinned");
      if (entry.roleLabel) {
        var rl = el("div", "rolelab", entry.roleLabel);
        row.appendChild(rl);
      }

      var who = el("button", "who" + (state.open[seat] ? " open" : ""));
      who.type = "button";
      who.title = "Show " + seat + "'s pool so far";
      who.appendChild(el("div", "nm", seat));
      who.appendChild(el("div", "tm", D.meta.teamNames[teamOf(seat)]));
      who.addEventListener("click", function () {
        if (state.open[seat]) delete state.open[seat]; else state.open[seat] = true;
        render();
      });
      row.appendChild(who);

      if (!found) {
        // Only reachable on a truncated log (a seat with fewer picks than the
        // pack length); keep the row so the ring stays visually intact.
        row.appendChild(el("div", "chip", "—"));
        row.appendChild(el("div"));
        var gap = el("div", "rest");
        gap.appendChild(el("span", "empty", "no pick recorded"));
        row.appendChild(gap);
        wrap.appendChild(row);
        return;
      }

      var b = found.booster, s = found.step;
      var chip = el("button", "chip" + (state.pinned === b.index ? " on" : ""));
      chip.type = "button";
      chip.appendChild(el("span", null, "pack"));
      chip.appendChild(el("span", "n", "#" + b.index));
      if (knowsFuture()) {
        chip.title = "Trace this booster (opened by " + b.opener + ")";
        chip.addEventListener("click", function () {
          state.pinned = state.pinned === b.index ? null : b.index;
          render();
        });
      } else {
        // Tracing reads a pack's future, which only the overhead camera has.
        chip.disabled = true;
        chip.title = "Tracing a booster is an overhead-view tool";
      }
      row.appendChild(chip);

      var taking = el("div", "taking");
      taking.appendChild(el("div", "lbl", s.taken.length > 1 ? "takes two" : "takes"));
      var shelf = el("div", "rest");
      if (hidingNow()) {
        s.taken.forEach(function () {
          var back = el("div", "cardback");
          back.appendChild(el("span", null, "?"));
          shelf.appendChild(back);
        });
      } else {
        s.taken.forEach(function (id) { shelf.appendChild(cardEl(id, { cls: "big live" })); });
      }
      taking.appendChild(shelf);
      row.appendChild(taking);

      var traced = knowsFuture() && state.pinned === b.index;
      var rest = el("div", "rest" + (traced ? " traced" : ""));
      var remaining = s.contents.filter(function (id) { return s.taken.indexOf(id) === -1; });
      if (!remaining.length) rest.appendChild(el("span", "empty", "last card"));

      if (traced) {
        // Tracing a pack means reading its future: sort what is left by the
        // pick that will claim it, and say who claims it.
        var when = b.takenAt || {};
        remaining = remaining.slice().sort(function (x, y) {
          return ((when[x] || {}).pick || 99) - ((when[y] || {}).pick || 99);
        });
        remaining.forEach(function (id) {
          var at = when[id];
          var cell = el("div", "tcard");
          cell.appendChild(cardEl(id, { cls: s.inserted.indexOf(id) !== -1 ? "ins" : "" }));
          var tag = el("div", "tlab");
          if (at) {
            tag.appendChild(el("span", "tp", "p" + at.pick));
            var whoTag = el("span", "tw", shortName(at.seat));
            whoTag.style.color = teamColor(at.seat);
            tag.appendChild(whoTag);
            cell.title = "taken at pick " + at.pick + " by " + at.seat;
          } else {
            tag.appendChild(el("span", "tp", "—"));
          }
          cell.appendChild(tag);
          rest.appendChild(cell);
        });
      } else {
        remaining.forEach(function (id) {
          rest.appendChild(cardEl(id, { cls: s.inserted.indexOf(id) !== -1 ? "ins" : "" }));
        });
      }
      row.appendChild(rest);
      wrap.appendChild(row);
      if (state.open[seat]) wrap.appendChild(renderPool(seat));
    });
    return wrap;
  }

  /* ---------------- camera ---------------- */

  function setMode(mode) {
    state.revealed = false;
    state.mode = mode;
    if (mode !== "overhead") state.pinned = null;   // future-only affordance
    state.card = null;                              // detail says different things per mode
    render();
  }

  function renderModeBar() {
    var wrap = el("div", "camera");

    var seg = el("div", "modes");
    MODES.forEach(function (m) {
      var b = el("button", "modebtn" + (state.mode === m.id ? " on" : ""), m.label);
      b.type = "button";
      b.title = m.hint;
      b.addEventListener("click", function () { setMode(m.id); });
      seg.appendChild(b);
    });
    wrap.appendChild(seg);

    // The seat picker only means anything when the camera is following someone.
    var pick = el("label", "seatpick" + (state.mode === "overhead" ? " off" : ""));
    pick.appendChild(el("span", null, "watching"));
    var sel = el("select");
    D.ring.forEach(function (seat) {
      var o = el("option", null, seat);
      o.value = seat;
      if (seat === state.focus) o.selected = true;
      sel.appendChild(o);
    });
    sel.disabled = state.mode === "overhead";
    sel.addEventListener("change", function () {
      state.focus = this.value;
      state.card = null;
      state.revealed = false;
      render();
    });
    pick.appendChild(sel);
    wrap.appendChild(pick);

    var hide = el("label", "hidepick");
    var box = el("input");
    box.type = "checkbox";
    box.checked = state.hidePicks;
    box.addEventListener("change", function () {
      state.hidePicks = this.checked;
      state.revealed = false;
      render();
    });
    hide.appendChild(box);
    hide.appendChild(el("span", null, "Hide picks"));
    hide.title = "Cover what each seat takes, so you can call the pick yourself";
    wrap.appendChild(hide);

    if (state.hidePicks) {
      var rv = el("button", "reveal" + (state.revealed ? " done" : ""),
                  state.revealed ? "revealed" : "Reveal pick");
      rv.type = "button";
      rv.disabled = state.revealed;
      rv.addEventListener("click", function () { state.revealed = true; render(); });
      wrap.appendChild(rv);
    }

    var m = MODES.filter(function (x) { return x.id === state.mode; })[0];
    wrap.appendChild(el("span", "camhint", m ? m.hint : ""));
    return wrap;
  }

  /* ---------------- first person ---------------- */

  /* One seat, and only what that seat could have known at this moment: the
     booster in front of them, what they took from it, and the pool they have
     built. No other seat, no future. */
  function renderFirstPerson() {
    var seat = state.focus;
    var wrap = el("div", "fp");
    var found = stepAt[state.pack + "|" + state.pick + "|" + seat];

    var head = el("div", "fphead");
    head.style.setProperty("--team", teamColor(seat));
    head.appendChild(el("div", "fpwho", seat));
    head.appendChild(el("div", "fptm", D.meta.teamNames[teamOf(seat)]));
    wrap.appendChild(head);

    if (!found) {
      wrap.appendChild(el("div", "empty", "no pick recorded for this seat"));
      return wrap;
    }
    var st = found.step;

    var seenBefore = {};
    for (var k = 1; k < state.pick; k++) {
      var f = stepAt[state.pack + "|" + k + "|" + seat];
      if (f) f.step.contents.forEach(function (id) { seenBefore[id] = true; });
    }

    /* What this player knows about THIS booster from having held it before.
       A drafter remembers what they passed, so when a pack comes back the
       cards that are missing are legitimately theirs to know -- they just
       don't know who took them. Cumulative across every earlier sighting,
       because with six seats and fifteen picks a pack can wheel twice. */
    var everSaw = {}, iTook = {};
    for (var k2 = 1; k2 < state.pick; k2++) {
      var f2 = stepAt[state.pack + "|" + k2 + "|" + seat];
      if (!f2 || f2.booster.index !== found.booster.index) continue;
      f2.step.contents.forEach(function (id) { everSaw[id] = true; });
      f2.step.taken.forEach(function (id) { iTook[id] = true; });
    }
    var stillHere = {};
    st.contents.forEach(function (id) { stillHere[id] = true; });
    var gone = Object.keys(everSaw).filter(function (id) {
      return !stillHere[id] && !iTook[id];
    });

    var sec = el("div", "fpsec");
    var wheeled = st.contents.filter(function (id) { return seenBefore[id]; });
    var lbl = el("div", "fplbl", "The booster in front of you");
    lbl.appendChild(el("span", "fpn", st.contents.length + " cards"));
    if (wheeled.length) {
      // Legitimate first-person knowledge: they have seen this pack before.
      lbl.appendChild(el("span", "fpwheel", wheeled.length + " wheeled back"));
    }
    sec.appendChild(lbl);
    var grid = el("div", "fpgrid");
    st.contents.forEach(function (id) {
      var cls = "big";
      // The highlight on the taken card is itself the answer, so it goes too.
      if (st.taken.indexOf(id) !== -1 && !hidingNow()) cls += " live";
      else if (seenBefore[id]) cls += " wheelback";
      if (st.inserted.indexOf(id) !== -1) cls += " ins";
      grid.appendChild(cardEl(id, { cls: cls }));
    });
    sec.appendChild(grid);
    wrap.appendChild(sec);

    if (gone.length) {
      var lost = el("div", "fpsec gone");
      var glbl = el("div", "fplbl", "Passed and didn\u2019t come back");
      glbl.appendChild(el("span", "fpn", gone.length + " of them"));
      lost.appendChild(glbl);
      var ggrid = el("div", "fpgrid small");
      // No "taken by" here on purpose: the player can see these are missing,
      // but not who took them.
      gone.forEach(function (id) { ggrid.appendChild(cardEl(id, { cls: "lost" })); });
      lost.appendChild(ggrid);
      wrap.appendChild(lost);
    }

    var take = el("div", "fpsec");
    var takeLbl = el("div", "fplbl", hidingNow() ? "Which do you take?" : "You take");
    take.appendChild(takeLbl);
    var tg = el("div", "fpgrid");
    if (hidingNow()) {
      st.taken.forEach(function () {
        var back = el("div", "cardback big");
        back.appendChild(el("span", null, "?"));
        tg.appendChild(back);
      });
      var rv2 = el("button", "reveal", "Reveal pick");
      rv2.type = "button";
      rv2.addEventListener("click", function () { state.revealed = true; render(); });
      tg.appendChild(rv2);
    } else {
      st.taken.forEach(function (id) { tg.appendChild(cardEl(id, { cls: "big live" })); });
    }
    take.appendChild(tg);
    wrap.appendChild(take);

    wrap.appendChild(renderPool(seat, true));
    return wrap;
  }

  /* ---------------- pool drawer ---------------- */

  function renderPool(seat, own) {
    var entries = poolThrough(seat);
    var box = el("div", "pool" + (own ? " own" : ""));
    box.style.setProperty("--team", teamColor(seat));

    var head = el("div", "poolhead");
    head.appendChild(el("strong", null, own ? "Your pool" : seat));
    head.appendChild(el("span", "count", entries.length +
      (entries.length === 1 ? " card" : " cards") + " through P" + state.pack +
      "p" + state.pick));

    var tally = colourTally(entries);
    var pips = el("span", "pips");
    COLOURS.forEach(function (pair) {
      if (!tally[pair[0]]) return;
      var pip = el("span", "pip pip-" + pair[0], pair[0] + " " + tally[pair[0]]);
      pip.title = pair[1] + ": " + tally[pair[0]];
      pips.appendChild(pip);
    });
    head.appendChild(pips);

    var sort = el("button", "poolsort",
      "sort: " + (state.poolSort === "draft" ? "draft" : "colour"));
    sort.type = "button";
    sort.title = "Switch between draft order and colour grouping";
    sort.addEventListener("click", function () {
      state.poolSort = state.poolSort === "draft" ? "colour" : "draft";
      render();
    });
    head.appendChild(sort);

    var shut = el("button", "poolclose", "Hide");
    shut.type = "button";
    shut.addEventListener("click", function () {
      delete state.open[seat];
      render();
    });
    head.appendChild(shut);
    box.appendChild(head);

    var order = entries.slice();
    if (state.poolSort === "colour") {
      var rank = {};
      COLOURS.forEach(function (pair, i) { rank[pair[0]] = i; });
      order.sort(function (a, b) {
        var ca = card(a.card), cb = card(b.card);
        var ka = ca.colors.length === 1 ? rank[ca.colors[0]]
               : ca.colors.length ? COLOURS.length : rank.C;
        var kb = cb.colors.length === 1 ? rank[cb.colors[0]]
               : cb.colors.length ? COLOURS.length : rank.C;
        return ka - kb || (ca.cmc || 0) - (cb.cmc || 0) ||
               ca.name.localeCompare(cb.name);
      });
    }

    var grid = el("div", "poolgrid");
    if (!order.length) grid.appendChild(el("span", "empty", "nothing drafted yet"));
    order.forEach(function (e) {
      var justTaken = e.pack === state.pack && e.pick === state.pick;
      grid.appendChild(cardEl(e.card, { cls: justTaken ? "live" : "" }));
    });
    box.appendChild(grid);
    return box;
  }

  /* ---------------- card detail ---------------- */

  function renderDetail() {
    if (!state.card) return null;
    var fate = D.fates[state.card];
    if (!fate) return null;
    var c = card(fate.card);

    var box = el("div", "detail");
    var close = el("button", "close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "close");
    close.addEventListener("click", function () { state.card = null; render(); });
    box.appendChild(close);

    if (c.normal) {
      var img = document.createElement("img");
      img.src = c.normal;
      img.alt = c.name;
      box.appendChild(img);
    }

    var body = el("div", "body");

    // The card's own printed detail. Faces come from Scryfall; a DFC has two,
    // and everything below renders per face so both halves are readable.
    // Without the enrichment (offline build) we still show what the log knows.
    var faces = c.faces && c.faces.length ? c.faces
              : [{ name: c.name, cost: c.cost, typeLine: c.type, oracle: "", pt: "" }];
    faces.forEach(function (f, i) {
      var head = el("div", "cardhead");
      head.appendChild(el("h3", null, f.name || c.name));
      if (f.cost) head.appendChild(el("span", "cost", f.cost));
      body.appendChild(head);

      var line = el("div", "typeline");
      line.appendChild(el("span", null, f.typeLine || c.type || ""));
      if (f.pt) line.appendChild(el("span", "pt", f.pt));
      body.appendChild(line);

      if (f.oracle) {
        var ora = el("div", "oracle");
        // Scryfall separates abilities with newlines; each is its own line so
        // reminder text does not run into the next ability.
        f.oracle.split("\n").forEach(function (para) {
          if (para.trim()) ora.appendChild(el("p", null, para));
        });
        body.appendChild(ora);
      }
      if (i < faces.length - 1) body.appendChild(el("div", "facerule"));
    });

    // The journey. Overhead knows the whole thing; the closer cameras know
    // only what the seat being watched has actually seen, so they get that
    // player's own memory of the card instead of its fate.
    var journey = el("div", "journey");
    if (knowsFuture()) {
      journey.appendChild(el("span", "mono",
        "P" + fate.pack + " #" + fate.booster +
        " · seen pick " + fate.firstSeen +
        " → taken pick " + fate.takenAt + " by " + fate.takenBy));
      if (fate.wheeled) journey.appendChild(el("span", "wheeled", "wheeled"));
      if (fate.passedBy.length) {
        var head2 = fate.passedBy.slice(0, 3).join(" → ");
        journey.appendChild(el("span", "passed",
          "passed by " + head2 + (fate.passedBy.length > 3 ? " →…" : "")));
      }
    } else {
      var mine = ownHistory(state.focus, fate.card);
      if (!mine.length) {
        journey.appendChild(el("span", "passed",
          shortName(state.focus) + " hasn\u2019t seen this card"));
      } else {
        var took = mine.filter(function (x) { return x.took; })[0];
        var first = mine[0];
        journey.appendChild(el("span", "mono", took
          ? "you took it \u2014 P" + took.pack + " pick " + took.pick
          : "you passed it \u2014 P" + first.pack + " pick " + first.pick));
        if (mine.length > 1) {
          journey.appendChild(el("span", "wheeled",
            "came back " + (mine.length - 1) + "\u00d7"));
        }
      }
    }
    body.appendChild(journey);

    if (knowsFuture()) {
      var jump = el("button", "jump", "Show this pick");
      jump.type = "button";
      jump.addEventListener("click", function () {
        state.pinned = fate.booster;
        setStep(fate.pack, fate.takenAt);
      });
      body.appendChild(jump);
    }

    box.appendChild(body);
    return box;
  }

  /* ---------------- search results ---------------- */

  function renderResults() {
    var host = document.getElementById("results");
    if (!host) return;
    host.innerHTML = "";
    var q = state.query.trim().toLowerCase();
    if (q.length < 2) { host.hidden = true; return; }

    // Overhead searches the whole cube. The closer cameras search only what
    // the watched seat has actually had in front of them, because a search hit
    // is itself information -- "that card exists in this draft" included.
    var seen = knowsFuture() ? null : seenBy(state.focus);
    var names = Object.keys(D.fates)
      .filter(function (n) {
        if (n.toLowerCase().indexOf(q) === -1) return false;
        return seen ? !!seen[D.fates[n].card] : true;
      })
      .sort()
      .slice(0, 12);
    host.hidden = !names.length;
    names.forEach(function (name) {
      var f = D.fates[name];
      var row = el("button", "row");
      row.type = "button";
      row.appendChild(el("span", null, name));
      if (seen) {
        var mine = ownHistory(state.focus, f.card);
        var took = mine.filter(function (x) { return x.took; })[0];
        var at = took || mine[0];
        row.appendChild(el("span", "mono",
          at ? "P" + at.pack + " pick " + at.pick : ""));
        row.appendChild(el("span", "mono", took ? "you took it" : "you passed it"));
        row.addEventListener("click", function () {
          state.card = name;
          if (at) setStep(at.pack, at.pick); else render();
        });
      } else {
        row.appendChild(el("span", "mono", "P" + f.pack + " #" + f.booster +
          " · pick " + f.takenAt + (f.wheeled ? " · wheeled" : "")));
        row.appendChild(el("span", "mono", f.takenBy));
        row.addEventListener("click", function () {
          state.pinned = f.booster;
          state.card = name;
          setStep(f.pack, f.takenAt);
        });
      }
      host.appendChild(row);
    });
  }

  /* ---------------- page ---------------- */

  function render() {
    var app = document.getElementById("app");
    app.innerHTML = "";
    var wrap = el("div", "wrap");

    var mast = el("div", "mast");
    var h1 = el("h1", null, D.meta.friendlyId);
    mast.appendChild(h1);
    mast.appendChild(el("span", "sub",
      [D.meta.cube, D.meta.started, SEATS + " seats", PACKS + " packs"]
        .filter(Boolean).join("  ·  ")));
    var key = el("div", "teamkey");
    ["A", "B"].forEach(function (t) {
      var s = el("span");
      var i = el("i");
      i.style.background = t === "A" ? "var(--team-a)" : "var(--team-b)";
      s.appendChild(i);
      s.appendChild(el("span", null, D.meta.teamNames[t]));
      key.appendChild(s);
    });
    mast.appendChild(key);
    wrap.appendChild(mast);

    wrap.appendChild(renderModeBar());
    wrap.appendChild(renderClock());

    var results = el("div", "results");
    results.id = "results";
    results.hidden = true;
    wrap.appendChild(results);

    if (state.mode === "first") {
      wrap.appendChild(renderFirstPerson());
    } else if (state.mode === "shoulder") {
      // Ordered the way the boosters travel: the seat feeding you sits above,
      // the seat you feed below, so the pack flows down the screen.
      var n = neighbours(state.focus, state.pack);
      wrap.appendChild(renderSeats([
        { seat: n.upstream,   role: "side",  roleLabel: "feeds " + shortName(state.focus) },
        { seat: state.focus,  role: "focus", roleLabel: "watching" },
        { seat: n.downstream, role: "side",  roleLabel: shortName(state.focus) + " feeds" }
      ]));
    } else {
      wrap.appendChild(renderSeats(D.ring.map(function (seat) {
        return { seat: seat };
      })));
    }

    var ev = eventPicks[state.pack + "|" + state.pick];
    if (ev) {
      var note = el("div", "events");
      note.appendChild(el("b", null, "This pick: "));
      note.appendChild(el("span", null, ev.description));
      wrap.appendChild(note);
    }

    wrap.appendChild(el("p", "hint", state.mode === "overhead"
      ? "←/→ step · click a name for that player\u2019s pool so far · click a pack chip to order it by when each card gets taken (click again to stop) · click any card for its fate"
      : state.mode === "shoulder"
      ? "←/→ step · you see the two seats either side of " + shortName(state.focus) +
        " as they are now — no future, no other seats · click a name for that player\u2019s pool"
      : "←/→ step · only what " + shortName(state.focus) +
        " could see at this moment · click any card for its text and your own history with it"));

    app.appendChild(wrap);

    var detail = renderDetail();
    if (detail) app.appendChild(detail);
    renderResults();
    writeHash();
  }

  /* ---------------- shareable position ---------------- */

  /* mode/seat/pack.pick in the hash, so a link can open on one player's seat at
     one moment rather than always at pack 1 pick 1. Written on every render;
     read once on load and whenever someone edits it or hits back. */
  var applyingHash = false;

  function writeHash() {
    if (applyingHash) return;
    var h = "#" + state.mode + "/" +
            (state.mode === "overhead" ? "-" : encodeURIComponent(state.focus)) +
            "/p" + state.pack + "." + state.pick;
    if (h !== location.hash) history.replaceState(null, "", h);
  }

  function readHash() {
    var raw = (location.hash || "").replace(/^#/, "");
    if (!raw) return false;
    var bits = raw.split("/");
    var mode = bits[0];
    if (!MODES.some(function (m) { return m.id === mode; })) return false;
    state.mode = mode;
    var seat = bits[1] ? decodeURIComponent(bits[1]) : "";
    if (seat && seat !== "-" && D.ring.indexOf(seat) !== -1) state.focus = seat;
    var at = /^p(\d+)\.(\d+)$/.exec(bits[2] || "");
    if (at) {
      state.pack = Math.min(Math.max(+at[1], 1), PACKS);
      state.pick = Math.min(Math.max(+at[2], 1), PICKS);
    }
    if (state.mode !== "overhead") state.pinned = null;
    return true;
  }

  window.addEventListener("hashchange", function () {
    applyingHash = true;
    readHash();
    applyingHash = false;
    render();
  });

  document.addEventListener("keydown", function (e) {
    if (/^(INPUT|TEXTAREA)$/.test(e.target.tagName)) return;
    if (e.key === "ArrowRight") { advance(1); e.preventDefault(); }
    else if (e.key === "ArrowLeft") { advance(-1); e.preventDefault(); }
    else if (e.key === "Escape") { state.card = null; state.pinned = null; render(); }
    // 1/2/3 walk the camera out from the player to the whole table
    else if (e.key === "1" || e.key === "2" || e.key === "3") {
      setMode(MODES[+e.key - 1].id);
      e.preventDefault();
    }
    else if (e.key === "h" || e.key === "H") {
      state.hidePicks = !state.hidePicks; state.revealed = false; render();
    }
    else if ((e.key === "r" || e.key === "R") && hidingNow()) {
      state.revealed = true; render();
    }
  });

  readHash();
  render();
})();
