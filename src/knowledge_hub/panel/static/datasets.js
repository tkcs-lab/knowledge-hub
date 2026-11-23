import { h, clear, fmt } from "/static/dom.js";
import { confirmButton, opForm } from "/static/ops.js";

const FLAGS = ["filterable", "embed", "sensitive", "indexed"];

function flagText(col) {
  return FLAGS.filter((f) => col[f]).join(", ");
}

function toggle(slot, build) {
  if (slot.firstChild) {
    clear(slot);
    return;
  }
  slot.appendChild(build());
}

function columnRow(ds, col, reload) {
  const slot = h("div", { class: "inline-form" });
  const edit = h("button", { type: "button" }, "Edit flags");
  edit.addEventListener("click", () =>
    toggle(slot, () =>
      h(
        "div",
        {},
        FLAGS.map((f) =>
          opForm({
            op: "dataset.set_flags",
            previewless: true,
            fields: [
              { name: "dataset", label: "Dataset", value: ds.name, readonly: true },
              { name: "column", label: "Column", value: col.name, readonly: true },
            ],
            fixed: { [f]: !col[f] },
            submitLabel: col[f] ? `Turn ${f} off` : `Turn ${f} on`,
            onApplied: reload,
          }).form
        ),
        opForm({
          op: "dataset.set_flags",
          previewless: true,
          summary: "Leave a field empty to keep it.",
          fields: [
            { name: "dataset", label: "Dataset", value: ds.name, readonly: true },
            { name: "column", label: "Column", value: col.name, readonly: true },
            { name: "label", label: "Label", value: col.label, omitEmpty: true, required: false },
            { name: "source_name", label: "Source name", value: col.source_name, omitEmpty: true, required: false },
          ],
          onApplied: reload,
        }).form
      )
    )
  );
  const rename = h("button", { type: "button" }, "Rename");
  rename.addEventListener("click", () =>
    toggle(slot, () =>
      opForm({
        op: "dataset.rename_column",
        previewless: true,
        fields: [
          { name: "dataset", label: "Dataset", value: ds.name, readonly: true },
          { name: "column", label: "Column", value: col.name, readonly: true },
          { name: "new_name", label: "New name" },
        ],
        onApplied: reload,
      }).form
    )
  );
  const drop = confirmButton(`Drop column ${ds.name}.${col.name}`, () => {
    clear(slot);
    slot.appendChild(
      opForm({
        op: "dataset.drop_column",
        previewless: true,
        fields: [
          { name: "dataset", label: "Dataset", value: ds.name, readonly: true },
          { name: "column", label: "Column", value: col.name, readonly: true },
        ],
        fixed: { confirm: true },
        submitLabel: `Drop ${col.name}`,
        confirmText: `Drop column ${col.name} and its data?`,
        onApplied: reload,
      }).form
    );
  });
  return h(
    "tr",
    {},
    h("td", {}, col.name),
    h("td", {}, col.type),
    h("td", {}, col.label),
    h("td", {}, flagText(col)),
    h("td", {}, col.source_name),
    h("td", { class: "row-actions" }, h("div", { class: "actions" }, edit, rename, drop), slot)
  );
}

function datasetSection(ds, reload) {
  const head = ["Column", "Type", "Label", "Flags", "Source name", "Actions"];
  const settings = h("div", { class: "inline-form" });
  const set = h("button", { type: "button" }, "Edit dataset");
  set.addEventListener("click", () =>
    toggle(settings, () =>
      opForm({
        op: "dataset.set",
        previewless: true,
        summary: "Audiences are comma separated; empty fields stay as they are.",
        fields: [
          { name: "dataset", label: "Dataset", value: ds.name, readonly: true },
          { name: "title", label: "Title", value: ds.title, omitEmpty: true, required: false },
          { name: "description", label: "Description", value: ds.description, omitEmpty: true, required: false },
          { name: "title_column", label: "Title column", value: ds.title_column, omitEmpty: true, required: false },
          { name: "audiences", label: "Audiences", value: ds.audiences.join(", "), list: true, omitEmpty: true, required: false },
        ],
        onApplied: reload,
      }).form
    )
  );
  const addSlot = h("div", { class: "inline-form" });
  const add = h("button", { type: "button" }, "Add column");
  add.addEventListener("click", () =>
    toggle(addSlot, () =>
      opForm({
        op: "dataset.add_column",
        previewless: true,
        summary: "Type is text, number, date, bool, choice or ref. Choices are comma separated; set the flags afterwards with Edit flags.",
        fields: [
          { name: "dataset", label: "Dataset", value: ds.name, readonly: true },
          { name: "column", label: "Column" },
          { name: "type", label: "Type" },
          { name: "label", label: "Label", omitEmpty: true, required: false },
          { name: "choices", label: "Choices", list: true, omitEmpty: true, required: false },
          { name: "ref", label: "Ref dataset", omitEmpty: true, required: false },
          { name: "source_name", label: "Source name", omitEmpty: true, required: false },
        ],
        onApplied: reload,
      }).form
    )
  );
  return h(
    "section",
    { class: "dataset" },
    h("h3", {}, `${ds.name} (${ds.title})`),
    h(
      "p",
      { class: "muted" },
      `Audiences: ${ds.audiences.join(", ")}. History: ${ds.history ? "on" : "off"}. Max outdated: ${ds.max_outdated_percent}%.`
    ),
    h("div", { class: "actions" }, set, add),
    settings,
    addSlot,
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, head.map((t) => h("th", {}, t)))),
      h("tbody", {}, ds.columns.map((c) => columnRow(ds, c, reload)))
    )
  );
}

async function load(body) {
  let data;
  try {
    data = await window.api("GET", "/api/datasets");
  } catch (err) {
    clear(body);
    body.appendChild(h("p", { class: "error" }, err.message));
    return;
  }
  const reload = () => load(body);
  clear(body);
  body.appendChild(h("h2", {}, "Datasets"));
  body.appendChild(h("h3", {}, "New dataset"));
  body.appendChild(
    opForm({
      op: "dataset.create",
      previewless: true,
      summary: "Audiences are comma separated group names.",
      fields: [
        { name: "dataset", label: "Name" },
        { name: "title", label: "Title" },
        { name: "audiences", label: "Audiences", list: true },
        { name: "description", label: "Description", omitEmpty: true, required: false },
      ],
      onApplied: reload,
    }).form
  );
  const head = ["Name", "Title", "Audiences", "Columns", "Rows (estimate)"];
  body.appendChild(
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, head.map((t) => h("th", {}, t)))),
      h(
        "tbody",
        {},
        data.datasets.map((d) =>
          h(
            "tr",
            {},
            h("td", {}, d.name),
            h("td", {}, d.title),
            h("td", {}, d.audiences.join(", ")),
            h("td", {}, fmt(d.columns.length)),
            h("td", {}, d.rows_estimate === null ? "unknown" : fmt(d.rows_estimate))
          )
        )
      )
    )
  );
  for (const ds of data.datasets) {
    body.appendChild(datasetSection(ds, reload));
  }
}

window.panelTabs = window.panelTabs || {};
window.panelTabs.datasets = (root) => {
  clear(root);
  const body = h("div", {});
  root.appendChild(body);
  load(body);
};
