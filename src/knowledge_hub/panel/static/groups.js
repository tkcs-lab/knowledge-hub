import { h, clear, fmt, when } from "/static/dom.js";
import { confirmButton, opForm, setConfigSha } from "/static/ops.js";

/**
 * Mounts a Preview-then-Apply form for one fixed change into `slot`.
 * `fixed` carries the arguments; nothing is applied before a successful Preview.
 */
export function stage(slot, ctx, op, fixed, summary) {
  clear(slot);
  slot.appendChild(opForm({ op, fields: [], fixed, summary, onApplied: ctx.onApplied }).form);
  slot.scrollIntoView({ block: "nearest" });
}

function chipButton(title, onClick) {
  const remove = h("button", { type: "button", class: "chip-remove", title, "aria-label": title }, "\u00d7");
  remove.addEventListener("click", onClick);
  return remove;
}

function chip(text, button) {
  return h("li", { class: "chip" }, h("span", {}, text), button);
}

function selectAdd(label, options, onPick) {
  const select = h("select", { "aria-label": label });
  select.appendChild(h("option", { value: "" }, label));
  for (const option of options) {
    select.appendChild(h("option", { value: option }, option));
  }
  const add = h("button", { type: "button" }, "Add");
  add.addEventListener("click", () => {
    if (select.value) {
      onPick(select.value);
    }
  });
  return h("span", { class: "add-select" }, select, add);
}

function figures(stats) {
  const rows = [
    ["Documents T0", fmt(stats.documents.T0)],
    ["Documents T0a", fmt(stats.documents.T0a)],
    ["Stale documents", fmt(stats.stale_documents)],
    ["Records", fmt(stats.records)],
    ["Outdated records", fmt(stats.outdated_records)],
    ["Chunks T0", fmt(stats.chunks.T0)],
    ["Chunks T0a", fmt(stats.chunks.T0a)],
    ["Chunks T3", fmt(stats.chunks.T3)],
    [
      "Embedded chunks",
      `${fmt(stats.embedded_chunks)} of ${fmt(stats.chunks.T0 + stats.chunks.T0a + stats.chunks.T3)}`,
    ],
    ["Document characters", fmt(stats.document_chars)],
    ["Last indexed", when(stats.last_indexed_at)],
    ["Last ingested", when(stats.last_ingested_at)],
  ];
  return h(
    "table",
    { class: "figures" },
    h("tbody", {}, rows.map(([k, v]) => h("tr", {}, h("th", {}, k), h("td", {}, v))))
  );
}

function card(group, data, ctx, notice) {
  const run = (op, args, summary) => stage(notice, ctx, op, args, summary);
  const approvers = new Set(group.approvers);
  const memberChips = group.members.map((login) => {
    const isApprover = approvers.has(login);
    const title = `Remove ${login} from ${group.name}`;
    const button = isApprover
      ? confirmButton(
          "\u00d7",
          () => run("member.remove", { group: group.name, login, with_approver: true }, title),
          { class: "chip-remove", title, "aria-label": title }
        )
      : chipButton(title, () => run("member.remove", { group: group.name, login, with_approver: false }, title));
    return chip(isApprover ? `${login} (approver)` : login, button);
  });
  const approverChips = group.approvers.map((login) => {
    const title = `Remove ${login} as approver of ${group.name}`;
    return chip(login, chipButton(title, () => run("approver.remove", { group: group.name, login }, title)));
  });
  const people = data.people.map((p) => p.login).filter((l) => !group.members.includes(l));
  const directory = Object.entries(group.directory || {});
  const toggle = group.archived ? "group.unarchive" : "group.archive";
  const toggleButton = h("button", { type: "button" }, group.archived ? "Unarchive" : "Archive");
  toggleButton.addEventListener("click", () => run(toggle, { name: group.name }, `${group.archived ? "Unarchive" : "Archive"} group ${group.name}`));
  return h(
    "article",
    { class: "card" },
    h(
      "header",
      { class: "card-head" },
      h("h3", {}, group.name),
      h("span", { class: "muted" }, group.repo),
      h("span", { class: group.archived ? "muted" : "ok" }, group.archived ? "archived" : "active"),
      h("span", { class: "muted" }, group.slack_channel ? `Slack ${group.slack_channel}` : "no Slack channel")
    ),
    figures(group.stats),
    h("h4", {}, "Members"),
    h("ul", { class: "chips" }, memberChips),
    selectAdd("Add member...", people, (login) => run("member.add", { group: group.name, login }, `Add ${login} to ${group.name}`)),
    h("h4", {}, "Approvers"),
    h("ul", { class: "chips" }, approverChips),
    selectAdd("Add approver...", group.members.filter((l) => !approvers.has(l)), (login) =>
      run("approver.add", { group: group.name, login }, `Make ${login} an approver of ${group.name}`)
    ),
    h("h4", {}, "From the directory (read only)"),
    directory.length
      ? h(
          "ul",
          {},
          directory.map(([provider, ref]) =>
            h("li", {}, `${provider}: members ${ref.members.join(", ") || "none"}; approvers ${ref.approvers.join(", ") || "none"}`)
          )
        )
      : h("p", { class: "muted" }, "none"),
    h(
      "div",
      { class: "actions" },
      toggleButton,
      confirmButton(`Remove group ${group.name}`, () => run("group.remove", { name: group.name, confirm: true }, `Remove group ${group.name}`))
    )
  );
}

function peopleSection(data, ctx, notice) {
  const rows = data.people.map((p) =>
    h(
      "tr",
      {},
      h("td", {}, p.login),
      h("td", {}, p.email),
      h(
        "td",
        {},
        confirmButton(`Remove person ${p.login}`, () =>
          stage(notice, ctx, "person.remove", { login: p.login, from_groups: true }, `Remove person ${p.login} from all groups`)
        )
      )
    )
  );
  return h(
    "div",
    {},
    h("h3", {}, "People"),
    h(
      "table",
      {},
      h("thead", {}, h("tr", {}, ["Login", "Email", "Actions"].map((t) => h("th", {}, t)))),
      h("tbody", {}, rows)
    ),
    h("h4", {}, "Add a person"),
    opForm({
      op: "person.add",
      fields: [
        { name: "login", label: "Login" },
        { name: "email", label: "Email" },
      ],
      onApplied: ctx.onApplied,
    }).form
  );
}

async function load(root, notice, body) {
  let data;
  try {
    data = await window.api("GET", "/api/groups");
  } catch (err) {
    clear(body);
    body.appendChild(h("p", { class: "error" }, err.message));
    return;
  }
  setConfigSha(data.config_sha256);
  const ctx = { sha: data.config_sha256, onApplied: () => load(root, notice, body) };
  clear(body);
  body.appendChild(h("h2", {}, "Groups"));
  body.appendChild(h("div", { class: "cards" }, data.groups.map((g) => card(g, data, ctx, notice))));
  body.appendChild(h("h3", {}, "Add a group"));
  body.appendChild(
    opForm({
      op: "group.add",
      summary: "Members and approvers are comma separated logins.",
      fields: [
        { name: "name", label: "Name" },
        { name: "members", label: "Members", list: true, required: false },
        { name: "approvers", label: "Approvers", list: true, required: false },
      ],
      onApplied: ctx.onApplied,
    }).form
  );
  body.appendChild(peopleSection(data, ctx, notice));
}

window.panelTabs = window.panelTabs || {};
window.panelTabs.groups = (root) => {
  clear(root);
  const notice = h("div", { class: "op-status", "aria-live": "polite" });
  const body = h("div", {});
  root.appendChild(notice);
  root.appendChild(body);
  load(root, notice, body);
};
