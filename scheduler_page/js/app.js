import { apiFromHash, LinkExpired, NEEDS_UPDATE } from "./api.js";
import { mountUsual } from "./usual.js";
import { expiredMessage, loadFailedMessage } from "./messages.js";

const root = document.getElementById("app");
const say = (text) => { root.replaceChildren(Object.assign(document.createElement("p"), { className: "notice", textContent: text })); };

async function main() {
  try {
    const { api } = apiFromHash(location.hash);
    const me = await api.me();
    if (me.view === "usual") return await mountUsual(root, api);
    say(NEEDS_UPDATE);
  } catch (e) {
    if (e instanceof LinkExpired) say(expiredMessage());
    else { console.error(e); say(loadFailedMessage(e)); }
  }
}

addEventListener("hashchange", () => location.reload());
main();
