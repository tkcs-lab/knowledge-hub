import { h, clear } from "/static/dom.js";
import { opForm } from "/static/ops.js";
import { when } from "/static/groups.js";

const REFRESH_MS = 15000;
const FAST_MS = 3000;
const WATCH_MS = 90000;
const LABELS = { ok: "OK", warn: "Warning", fail: "Failing", "n/a": "Not applicable" };

let timer = null;
let baseSha = null;
let watchUntil = 0;

// Remember the config the page started with; a different one later means an Apply happened.
if (window.api) {
  window.api("GET", "/api/me").then((me) => {
    baseSha = baseSha || me.config_sha256 || null;
  }).catch(() => {});
}

function statusClass(status) {
  return LABELS[status] ? String(status).replace("/", "") : "warn";
}

function renderChecks(body, data) {
  clear(body);
  body.appendChild(h("h2", {}, "Health"));
  body.appendChild(h("p", { class: "muted" }, `Checked ${when(data.generated_at)}`));
  body.appendChild(
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, ["Check", "Status", "Detail"].map((t) => h("th", {}, t)))),
      h(
        "tbody",
        {},
        (data.checks || []).map((c) =>
          h(
            "tr",
            { "data-check": c.id },
            h("td", {}, c.id),
            h("td", { class: "status " + statusClass(c.status) }, LABELS[c.status] || c.status),
            h("td", {}, c.detail || "")
          )
        )
      )
    )
  );
}

window.panelTabs = window.panelTabs || {};
window.panelTabs.health = (root) => {
  clear(root);
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
  const body = h("div", {});
  const tools = h("div", { class: "actions" });
  const note = h("p", { class: "muted", "aria-live": "polite" });
  root.appendChild(body);
  root.appendChild(h("h3", {}, "Slow checks"));
  root.appendChild(tools);
  root.appendChild(note);

  const startWatch = () => {
    watchUntil = Date.now() + WATCH_MS;
  };
  for (const [op, label] of [
    ["health.setup_plan", "Run kh setup plan"],
    ["health.index_check", "Run kh check"],
  ]) {
    const form = opForm({ op, fields: [], previewless: true });
    form.form.querySelector("button[type=submit]").textContent = label;
    tools.appendChild(form.form);
  }

  const tick = async () => {
    if (!root.isConnected || root.hidden) {
      timer = null;
      return;
    }
    let hubOk = false;
    try {
      const me = await window.api("GET", "/api/me");
      if (baseSha && me.config_sha256 && me.config_sha256 !== baseSha) {
        baseSha = me.config_sha256;
        startWatch();
      }
      const data = await window.api("GET", "/api/health");
      renderChecks(body, data);
      const hub = (data.checks || []).find((c) => c.id === "hub");
      hubOk = Boolean(hub && hub.status === "ok");
    } catch (err) {
      clear(body);
      body.appendChild(h("p", { class: "error" }, err.message));
    }
    const watching = Date.now() < watchUntil && !hubOk;
    if (!watching) {
      watchUntil = 0;
    }
    note.textContent = watching ? "Waiting for kh serve to come back after the config change..." : "";
    timer = setTimeout(tick, watching ? FAST_MS : REFRESH_MS);
  };
  tick();
};
