// The draftbot.fyi app bar shown at the top of every view.
import { el } from "./dom.js";

export function appBar(context) {
  return el("header", { class: "appbar" },
    el("span", { class: "brand", text: "draftbot.fyi" }),
    el("span", { class: "context", text: context }),
  );
}
