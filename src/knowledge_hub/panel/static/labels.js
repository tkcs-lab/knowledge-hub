import { h, clear, fmt } from "/static/dom.js";
import { opForm, confirmButton, setConfigSha } from "/static/ops.js";

async function load(root) {
  let data;
  try {
    data = await window.api("GET", "/api/labels");
  } catch (err) {
    clear(root);
    root.appendChild(h("p", { class: "error" }, err.message));
    return;
  }
  setConfigSha(data.config_sha256);
  const reload = () => load(root);
  const rows = data.labels.map((label) => row(label, reload));
  clear(root);
  root.appendChild(h("h2", {}, "Labels"));
  root.appendChild(
    h(
      "table",
      {},
      h(
        "thead",
        {},
        h("tr", {}, ["Name", "Description", "State", "Documents", "Records", "Tech repos", "Actions"].map((t) => h("th", {}, t)))
      ),
      h("tbody", {}, rows)
    )
  );
  root.appendChild(h("h3", {}, "Add a label"));
  root.appendChild(
    opForm({
      op: "label.add",
      summary: "Adds a label to kh.yaml.",
      fields: [
        { name: "name", label: "Name" },
        { name: "description", label: "Description" },
      ],
      onApplied: reload,
    }).form
  );
}

function row(label, reload) {
  const usage = label.documents + label.records + label.tech_repos.length;
  const slot = h("div", { class: "inline-form" });
  const describe = h("button", { type: "button" }, "Describe");
  describe.addEventListener("click", () => {
    if (slot.firstChild) {
      clear(slot);
      return;
    }
    slot.appendChild(
      opForm({
        op: "label.describe",
        fields: [
          { name: "name", label: "Name", value: label.name, readonly: true },
          { name: "description", label: "Description", value: label.description },
        ],
        onApplied: reload,
      }).form
    );
  });
  const toggle = confirmButton(label.archived ? "Unarchive" : "Archive", () => {
    clear(slot);
    slot.appendChild(
      opForm({
        op: label.archived ? "label.unarchive" : "label.archive",
        fields: [{ name: "name", label: "Name", value: label.name, readonly: true }],
        onApplied: reload,
      }).form
    );
  });
  const reason = usage
    ? `In use: ${fmt(label.documents)} documents, ${fmt(label.records)} records, ${label.tech_repos.length} tech repos`
    : "";
  const remove = confirmButton(
    "Remove",
    () => {
      clear(slot);
      slot.appendChild(
        opForm({
          op: "label.remove",
          fields: [{ name: "name", label: "Name", value: label.name, readonly: true }],
          onApplied: reload,
        }).form
      );
    },
    { disabled: usage > 0, title: reason }
  );
  return h(
    "tr",
    {},
    h("td", {}, label.name),
    h("td", {}, label.description),
    h("td", { class: label.archived ? "muted" : "ok" }, label.archived ? "archived" : "active"),
    h("td", {}, fmt(label.documents)),
    h("td", {}, fmt(label.records)),
    h("td", {}, label.tech_repos.join(", ")),
    h("td", { class: "row-actions" }, h("div", { class: "actions" }, describe, toggle, remove), slot)
  );
}

window.panelTabs = window.panelTabs || {};
window.panelTabs.labels = (root) => load(root);
