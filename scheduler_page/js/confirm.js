// The confirmation panel: the "my usual counts as confirmed" switch and, with it off, one status row per league
// week with a button to confirm that week.
import { el } from "./dom.js";
import { CONFIRMATION_FORMAT, LinkExpired, checkDoc } from "./api.js";
import { expiredMessage } from "./messages.js";
import { addDays, dayName, leagueMidnight } from "./rounds.js";

export const SWITCH_LABEL = "Show my usual as confirmed";
export const SWITCH_HINT = 'On: your usual week counts as confirmed every round. Off: it shows as "probably" until you confirm the round.';
export const CONFIRM_BUTTON = "Looks right, confirm";
export const CONFIRM_NOTE = "Editing any day in a round confirms that round.";
export const SAVE_FAILED = "Couldn't save that. Check your connection and try again.";

// A league week named for its round, or by its deadline Sunday when it has no number.
export const weekStatusLabel = (monday, number) =>
  number == null ? `Week ending ${dayName(addDays(monday, 6))}` : `Round ${number}`;

// One row per league week (ascending Mondays). `number` names the week only when there is exactly one.
export function statusRows(doc, weeks, number, now) {
  const confirmedWeeks = new Set(doc.confirmed_rounds);
  return weeks.map((monday) => {
    const confirmed = confirmedWeeks.has(monday);
    const label = weekStatusLabel(monday, weeks.length === 1 ? number : null);
    return {
      monday,
      text: `${label}: ${confirmed ? "Confirmed ✓" : "Not confirmed"}`,
      confirmed,
      canConfirm: !confirmed && leagueMidnight(addDays(monday, 7)) > now,
    };
  });
}

// onChange runs after the switch or a confirmation is saved, so the page around the panel can redraw.
export async function mountConfirmPanel(root, api, { weeks, number, now, onChange }) {
  const range = { from: weeks[0], to: weeks.at(-1) };
  let doc = checkDoc(await api.getConfirmation(range), CONFIRMATION_FORMAT);

  const box = el("input", { type: "checkbox", id: "confirm-switch" });
  const note = el("p", { class: "status", role: "status", "aria-live": "polite" });
  const rows = el("div", { class: "confirmrows" });
  const panel = el("section", { class: "confirmpanel" },
    el("label", { class: "confirmswitch", for: "confirm-switch" }, box, el("span", { text: SWITCH_LABEL })),
    el("p", { class: "meta", text: SWITCH_HINT }),
    rows, note);

  function draw() {
    box.checked = doc.trusts_usual !== false;
    if (box.checked) { rows.findButtons = () => []; rows.replaceChildren(); return; }
    const buttons = [];
    const kids = statusRows(doc, weeks, number, now).map((r) => {
      const row = el("div", { class: "confirmrow" }, el("span", { text: r.text }));
      if (r.canConfirm) {
        const button = el("button", { type: "button", class: "primary", text: CONFIRM_BUTTON });
        button.addEventListener("click", () => confirm(r.monday));
        buttons.push(button);
        row.append(button);
      }
      return row;
    });
    rows.findButtons = () => buttons;
    rows.replaceChildren(...kids, el("p", { class: "meta", text: CONFIRM_NOTE }));
  }

  // One call at a time: the controls are disabled while it is pending, and a second request is ignored.
  let busy = false;
  const setBusy = (on) => {
    busy = on;
    box.disabled = on;
    for (const b of rows.findButtons?.() ?? []) b.disabled = on;
  };
  async function run(call, { announce, failure = SAVE_FAILED, refocus = false } = {}) {
    if (busy) return;
    setBusy(true);
    let ok = false;
    try { doc = checkDoc(await call(), CONFIRMATION_FORMAT); ok = true; } catch (e) { note.textContent = e instanceof LinkExpired ? expiredMessage() : failure; }
    draw();
    setBusy(false);
    if (ok && announce) note.textContent = announce();
    else if (ok && !announce) note.textContent = "";
    if (refocus) box.focus?.();
    if (ok) onChange?.();
  }
  const rowText = (monday) => statusRows(doc, weeks, number, now).find((r) => r.monday === monday).text;
  const confirm = (monday) => run(() => api.confirmRound(monday, range), { announce: () => rowText(monday), refocus: true });
  box.addEventListener("change", () => run(() => api.setTrustsUsual(box.checked, range), {
    announce: () => `${SWITCH_LABEL}: ${doc.trusts_usual !== false ? "on" : "off"}`,
  }));

  draw();
  root.replaceChildren(panel);
  // A failed read leaves whatever the note says; a good one doesn't clear a visible failure.
  const reload = async () => {
    if (busy) return;
    try { doc = checkDoc(await api.getConfirmation(range), CONFIRMATION_FORMAT); draw(); } catch (e) { if (e instanceof LinkExpired) note.textContent = expiredMessage(); }
  };
  return { reload };
}
