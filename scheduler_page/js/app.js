import { loadLink, getJson, putJson, LinkExpired } from "./api.js";
import { mountUsual, EXPIRED_MESSAGE } from "./usual.js";

const root = document.getElementById("app");
const say = (text) => { root.replaceChildren(Object.assign(document.createElement("p"), { className: "notice", textContent: text })); };

async function main() {
  try {
    const link = await loadLink(location.hash);
    if (!link.fixture && link.exp && Date.parse(link.exp) < Date.now()) return say(EXPIRED_MESSAGE);
    if (link.view === "usual") return await mountUsual(root, link, { getJson, putJson });
    say("This page needs updating.");
  } catch (e) {
    if (e instanceof LinkExpired) say(EXPIRED_MESSAGE);
    else { console.error(e); say("Something went wrong loading this page. Try the link again."); }
  }
}

addEventListener("hashchange", () => location.reload());
main();
