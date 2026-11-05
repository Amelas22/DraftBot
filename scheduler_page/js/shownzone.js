// The zone a heatmap page shows its times in. The viewer picks it at the top of the page and this browser remembers
// it; until they do, it is their usual week's zone, else the browser's. It changes only how times are shown: a usual
// week keeps the zone it was painted in.
import { zoneSelect } from "./usual.js";

const KEY = "draftbot.shownZone";

// localStorage, or null where the browser refuses it (some private modes throw on reading it at all).
function browserStorage() {
  try { return globalThis.localStorage ?? null; } catch { return null; }
}

// The remembered pick when it is a zone in zones, else fallback.
export function shownZone(fallback, zones, storage = browserStorage()) {
  let picked = null;
  try { picked = storage?.getItem(KEY) ?? null; } catch { /* unreadable: use the fallback */ }
  return picked && zones.includes(picked) ? picked : fallback;
}

// Remember zone as this browser's pick; a browser that refuses keeps it for this page only.
export function rememberZone(zone, storage = browserStorage()) {
  try { storage?.setItem(KEY, zone); } catch { /* full or refused: nothing to keep it in */ }
}

const allZones = (fallback) => (typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [fallback]);

// The "Show times in" picker for a page's app bar, opened on shownZone. onChange(zone) runs after the pick is
// remembered. Returns { zone, row }: the zone to draw in first, and the picker to place.
// deps: { zones?: string[], storage? } for tests.
export function shownZonePicker(fallback, onChange, deps = {}) {
  const zones = deps.zones || allZones(fallback);
  const storage = "storage" in deps ? deps.storage : browserStorage();
  const zone = shownZone(fallback, zones, storage);
  const { select, row } = zoneSelect(zone, zones, fallback, { id: "shown-zone", label: "Show times in" });
  select.addEventListener("change", () => {
    rememberZone(select.value, storage);
    onChange(select.value);
  });
  row.classList.add("shownzone");
  return { zone, row };
}
