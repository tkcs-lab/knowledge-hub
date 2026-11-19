import { h, clear, fmt } from "/static/dom.js";
import { confirmButton, setConfigSha } from "/static/ops.js";
import { runOp, argsForm, splitList, when } from "/static/groups.js";

function row(repo, ctx, notice) {
  const slot = h("div", { class: "inline-form" });
  const actions = h("div", { class: "actions" });
  if (repo.kind === "tech") {
    const edit = h("button", { type: "button" }, "Edit");
    edit.addEventListener("click", () => {
      if (slot.firstChild) {
        clear(slot);
        return;
      }
      const dirs = h("input", { type: "text", value: (repo.dirs || []).join(", ") });
      const labels = h("input", { type: "text", value: (repo.labels || []).join(", ") });
      const save = h("button", { type: "submit" }, "Save");
      const form = h(
        "form",
        { class: "op-form" },
        h("label", { class: "field" }, h("span", {}, "Dirs (comma separated)"), dirs),
        h("label", { class: "field" }, h("span", {}, "Labels (comma separated)"), labels),
        h("div", { class: "actions" }, save)
      );
      form.addEventListener("submit", (ev) => {
        ev.preventDefault();
        runOp(notice, "repo.set", { name: repo.name, dirs: splitList(dirs.value), labels: splitList(labels.value) }, ctx);
      });
      slot.appendChild(form);
    });
    actions.appendChild(edit);
    actions.appendChild(
      confirmButton(`Remove repo ${repo.name}`, () => runOp(notice, "repo.remove", { name: repo.name, confirm: true }, ctx))
    );
  }
  return h(
    "tr",
    {},
    h("td", {}, repo.name),
    h("td", {}, repo.kind === "group" ? `group ${repo.group}${repo.archived ? " (archived)" : ""}` : "tech"),
    h("td", {}, (repo.dirs || []).join(", ")),
    h("td", {}, (repo.labels || []).join(", ")),
    h("td", {}, fmt(repo.documents)),
    h("td", {}, repo.last_indexed_sha ? repo.last_indexed_sha.slice(0, 8) : ""),
    h("td", {}, repo.indexed_at ? when(repo.indexed_at) : ""),
    h("td", { class: "row-actions" }, actions, slot)
  );
}

async function load(notice, body) {
  let data;
  try {
    data = await window.api("GET", "/api/repos");
  } catch (err) {
    clear(body);
    body.appendChild(h("p", { class: "error" }, err.message));
    return;
  }
  setConfigSha(data.config_sha256);
  const ctx = { sha: data.config_sha256, onApplied: () => load(notice, body) };
  clear(body);
  body.appendChild(h("h2", {}, "Repos"));
  if (data.tech_org) {
    body.appendChild(h("p", { class: "muted" }, `Tech org: ${data.tech_org}. Group repos are read only.`));
  }
  const head = ["Name", "Kind", "Dirs", "Labels", "Documents", "Last indexed sha", "Indexed", "Actions"];
  body.appendChild(
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, head.map((t) => h("th", {}, t)))),
      h("tbody", {}, data.repos.map((r) => row(r, ctx, notice)))
    )
  );
  body.appendChild(h("h3", {}, "Add a repo"));
  body.appendChild(
    argsForm({
      op: "repo.add",
      summary: "Dirs and labels are comma separated.",
      fields: [
        { name: "name", label: "Name" },
        { name: "dirs", label: "Dirs", list: true, omitEmpty: true, required: false },
        { name: "labels", label: "Labels", list: true, omitEmpty: true, required: false },
      ],
      ctx,
    })
  );
}

window.panelTabs = window.panelTabs || {};
window.panelTabs.repos = (root) => {
  clear(root);
  const notice = h("div", { class: "op-status", "aria-live": "polite" });
  const body = h("div", {});
  root.appendChild(notice);
  root.appendChild(body);
  load(notice, body);
};
