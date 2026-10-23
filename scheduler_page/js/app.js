import { loadLink, getJson, putJson, LinkExpired, BrokenLink, NEEDS_UPDATE } from "./api.js";
import { mountLfg } from "./lfg.js";
import { mountUsual } from "./usual.js";
import { expiredMessage, brokenMessage, loadFailedMessage } from "./messages.js";

const root = document.getElementById("app");
const say = (text) => { root.replaceChildren(Object.assign(document.createElement("p"), { className: "notice", textContent: text })); };

// Stub until the league-match view lands.
const mountMatch = (root) => say("Coming soon.");

async function main() {
  let linkView; // unknown until file 8 is read: a link that fails before then gets the neutral wording
  try {
    const link = await loadLink(location.hash);
    linkView = link.view;
    if (!link.fixture && link.exp && Date.parse(link.exp) < Date.now()) return say(expiredMessage(linkView));
    if (link.view === "usual") return await mountUsual(root, link, { getJson, putJson });
    if (link.view === "lfg") return mountLfg(root, link, { getJson, putJson });
    if (link.view === "match") return mountMatch(root, link, { getJson, putJson });
    say(NEEDS_UPDATE);
  } catch (e) {
    if (e instanceof LinkExpired) say(expiredMessage(linkView));
    else if (e instanceof BrokenLink) say(brokenMessage(linkView));
    else { console.error(e); say(loadFailedMessage(e)); }
  }
}

addEventListener("hashchange", () => location.reload());
main();
