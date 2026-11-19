import { h, clear, fmt, when } from "/static/dom.js";
import { confirmButton, opForm, setConfigSha } from "/static/ops.js";
import { stage } from "/static/groups.js";

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
      slot.appendChild(
        opForm({
          op: "repo.set",
          fields: [
            { name: "name", label: "Name", value: repo.name, readonly: true },
            { name: "dirs", label: "Dirs (comma separated)", value: (repo.dirs || []).join(", "), list: true, omitEmpty: true, required: false },
            { name: "labels", label: "Labels (comma separated)", value: (repo.labels || []).join(", "), list: true, required: false },
          ],
          onApplied: ctx.onApplied,
        }).form
      );
    });
    actions.appendChild(edit);
    actions.appendChild(
      confirmButton(`Remove repo ${repo.name}`, () => stage(notice, ctx, "repo.remove", { name: repo.name, confirm: true }, `Remove repo ${repo.name}`))
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
    opForm({
      op: "repo.add",
      summary: "Dirs and labels are comma separated.",
      fields: [
        { name: "name", label: "Name" },
        { name: "dirs", label: "Dirs", list: true, omitEmpty: true, required: false },
        { name: "labels", label: "Labels", list: true, omitEmpty: true, required: false },
      ],
      onApplied: ctx.onApplied,
    }).form
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
