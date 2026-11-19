import { h, clear, fmt } from "/static/dom.js";
import { confirmButton, setConfigSha, BUSY_MESSAGE, RESTART_MESSAGE } from "/static/ops.js";

const POLL_MS = 1000;

export function when(value) {
  if (!value) {
    return "never";
  }
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? String(value) : d.toLocaleString();
}

export function splitList(text) {
  return String(text || "")
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s);
}

async function submit(op, args, dryRun, sha) {
  const body = { op, args, dry_run: dryRun };
  if (sha) {
    body.config_sha256 = sha;
  }
  let started;
  try {
    started = await window.api("POST", "/api/ops", body);
  } catch (err) {
    throw new Error(/busy/i.test(err.message) ? BUSY_MESSAGE : err.message);
  }
  for (;;) {
    const job = await window.api("GET", `/api/ops/${started.id}`);
    if (job.state === "done") {
      return job;
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
}

function show(status, text, cls, output) {
  clear(status);
  status.appendChild(h("p", { class: cls || "" }, text));
  if (output) {
    status.appendChild(h("pre", { class: "op-output" }, output));
  }
}

/**
 * Runs one op and reports into `status`; calls onApplied after a successful apply.
 * Returns true when the op succeeded.
 */
export async function runOp(status, op, args, ctx, dryRun = false) {
  show(status, dryRun ? "Previewing..." : "Applying...", "muted");
  try {
    const job = await submit(op, args, dryRun, ctx.sha);
    const output = job.output || "";
    if (output.includes("admin: cannot lock")) {
      show(status, BUSY_MESSAGE, "warn", output);
      return false;
    }
    if (job.ok === false || (job.exit_code !== null && job.exit_code !== undefined && job.exit_code !== 0)) {
      show(status, dryRun ? "Preview failed" : "Apply failed", "fail", output);
      return false;
    }
    show(status, dryRun ? "Preview succeeded" : "Applied", "ok", output);
    if (!dryRun && job.restart_expected) {
      status.appendChild(h("p", { class: "warn" }, RESTART_MESSAGE));
    }
    if (!dryRun && ctx.onApplied) {
      ctx.onApplied();
    }
    return true;
  } catch (err) {
    show(status, err.message, "error");
    return false;
  }
}

/**
 * Form with text fields; `list: true` fields are comma lists sent as arrays.
 * Apply stays disabled until a Preview of the same values succeeded.
 */
export function argsForm({ op, fields, summary, ctx }) {
  const inputs = {};
  const rows = [];
  for (const field of fields) {
    const input = h("input", { type: "text", name: field.name, required: field.required !== false });
    inputs[field.name] = input;
    rows.push(h("label", { class: "field" }, h("span", {}, field.label), input));
  }
  const status = h("div", { class: "op-status", "aria-live": "polite" });
  const preview = h("button", { type: "button" }, "Preview");
  const apply = h("button", { type: "submit", disabled: true }, "Apply");
  const form = h(
    "form",
    { class: "op-form" },
    summary ? h("p", { class: "muted" }, summary) : null,
    rows,
    h("div", { class: "actions" }, preview, apply),
    status
  );
  let previewed = null;
  let busy = false;
  const args = () => {
    const out = {};
    for (const field of fields) {
      const value = inputs[field.name].value;
      if (field.list) {
        const items = splitList(value);
        if (items.length || !field.omitEmpty) {
          out[field.name] = items;
        }
      } else if (value.trim() !== "" || field.required !== false) {
        out[field.name] = value.trim();
      }
    }
    return out;
  };
  const sync = () => {
    apply.disabled = busy || previewed !== JSON.stringify(args());
    preview.disabled = busy;
  };
  const go = async (dryRun) => {
    const snapshot = JSON.stringify(args());
    busy = true;
    sync();
    const ok = await runOp(status, op, JSON.parse(snapshot), ctx, dryRun);
    busy = false;
    previewed = dryRun && ok ? snapshot : null;
    sync();
  };
  preview.addEventListener("click", () => go(true));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (!apply.disabled) {
      go(false);
    }
  });
  form.addEventListener("input", sync);
  sync();
  return form;
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
  const run = (op, args) => runOp(notice, op, args, ctx);
  const approvers = new Set(group.approvers);
  const memberChips = group.members.map((login) => {
    const isApprover = approvers.has(login);
    const title = `Remove ${login} from ${group.name}`;
    const button = isApprover
      ? confirmButton(
          "\u00d7",
          () => run("member.remove", { group: group.name, login, with_approver: true }),
          { class: "chip-remove", title, "aria-label": title }
        )
      : chipButton(title, () => run("member.remove", { group: group.name, login, with_approver: false }));
    return chip(isApprover ? `${login} (approver)` : login, button);
  });
  const approverChips = group.approvers.map((login) => {
    const title = `Remove ${login} as approver of ${group.name}`;
    return chip(login, chipButton(title, () => run("approver.remove", { group: group.name, login })));
  });
  const people = data.people.map((p) => p.login).filter((l) => !group.members.includes(l));
  const directory = Object.entries(group.directory || {});
  const toggle = group.archived ? "group.unarchive" : "group.archive";
  const toggleButton = h("button", { type: "button" }, group.archived ? "Unarchive" : "Archive");
  toggleButton.addEventListener("click", () => run(toggle, { name: group.name }));
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
    selectAdd("Add member...", people, (login) => run("member.add", { group: group.name, login })),
    h("h4", {}, "Approvers"),
    h("ul", { class: "chips" }, approverChips),
    selectAdd("Add approver...", group.members.filter((l) => !approvers.has(l)), (login) =>
      run("approver.add", { group: group.name, login })
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
      confirmButton(`Remove group ${group.name}`, () => run("group.remove", { name: group.name, confirm: true }))
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
          runOp(notice, "person.remove", { login: p.login, from_groups: true }, ctx)
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
    argsForm({
      op: "person.add",
      fields: [
        { name: "login", label: "Login" },
        { name: "email", label: "Email" },
      ],
      ctx,
    })
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
    argsForm({
      op: "group.add",
      summary: "Members and approvers are comma separated logins.",
      fields: [
        { name: "name", label: "Name" },
        { name: "members", label: "Members", list: true, required: false },
        { name: "approvers", label: "Approvers", list: true, required: false },
      ],
      ctx,
    })
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
