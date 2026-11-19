import { h, clear } from "/static/dom.js";

export const BUSY_MESSAGE = "Another change is running; try again in a minute";
export const RESTART_MESSAGE = "kh serve restarts within about 20 seconds; Health shows when it is back";
const POLL_MS = 1000;

let lastSha = null;

export function setConfigSha(sha) {
  lastSha = sha || null;
}

async function post(body) {
  return window.api("POST", "/api/ops", body);
}

async function poll(id) {
  for (;;) {
    const job = await window.api("GET", `/api/ops/${id}`);
    if (job.state === "done") {
      return job;
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_MS));
  }
}

export function confirmButton(label, onConfirm, attrs) {
  const button = h("button", Object.assign({ type: "button" }, attrs || {}), label);
  button.addEventListener("click", () => {
    if (window.confirm(label + "?")) {
      onConfirm();
    }
  });
  return button;
}

export function splitList(text) {
  return String(text || "")
    .split(",")
    .map((s) => s.trim())
    .filter((s) => s);
}

/**
 * fields: [{name, label, value, readonly, required, list, omitEmpty}]
 *   list: true sends a comma list as an array; omitEmpty leaves an empty value out.
 * fixed: extra arguments sent as given (booleans, ids) without an input.
 * submitLabel renames Apply; confirmText asks window.confirm before Apply; onBusy(bool) reports a running request.
 * Preview posts dry_run: true; Apply waits for a successful Preview of the same arguments.
 */
export function opForm({
  op,
  fields,
  fixed = null,
  summary,
  previewless = false,
  onApplied = null,
  submitLabel = "Apply",
  confirmText = null,
  onBusy = null,
}) {
  const inputs = {};
  const rows = [];
  for (const field of fields) {
    const input = h("input", {
      type: "text",
      name: field.name,
      value: field.value || "",
      readonly: field.readonly || false,
      required: field.required === undefined ? true : field.required,
    });
    inputs[field.name] = input;
    rows.push(h("label", { class: "field" }, h("span", {}, field.label || field.name), input));
  }
  const status = h("div", { class: "op-status", "aria-live": "polite" });
  const preview = h("button", { type: "button" }, "Preview");
  const apply = h("button", { type: "submit", disabled: !previewless }, submitLabel);
  const form = h(
    "form",
    { class: "op-form" },
    summary ? h("p", { class: "muted" }, summary) : null,
    rows,
    h("div", { class: "actions" }, previewless ? null : preview, apply),
    status
  );

  let previewed = null; // {key, sha}
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
      } else if (value.trim() !== "" || !field.omitEmpty) {
        out[field.name] = value;
      }
    }
    return Object.assign(out, fixed || {});
  };
  const key = () => JSON.stringify(args());
  const sync = () => {
    apply.disabled = busy || (!previewless && !(previewed && previewed.key === key()));
    preview.disabled = busy;
  };
  const show = (text, cls, output) => {
    clear(status);
    status.appendChild(h("p", { class: cls || "" }, text));
    if (output) {
      status.appendChild(h("pre", { class: "op-output" }, output));
    }
  };

  async function run(dryRun) {
    const body = { op, args: args(), dry_run: dryRun };
    const sha = dryRun ? lastSha : previewed && previewed.sha;
    if (sha) {
      body.config_sha256 = sha;
    }
    if (!dryRun && confirmText && !window.confirm(confirmText)) {
      return;
    }
    busy = true;
    if (onBusy) {
      onBusy(true);
    }
    sync();
    show(dryRun ? "Previewing..." : "Applying...", "muted");
    try {
      const started = await post(body);
      const job = await poll(started.id);
      const output = job.output || "";
      if (output.includes("admin: cannot lock")) {
        show(BUSY_MESSAGE, "warn", output);
        previewed = null;
        return;
      }
      if (dryRun) {
        if (job.exit_code === 0 && job.ok !== false) {
          previewed = { key: JSON.stringify(body.args), sha: lastSha };
          show("Preview succeeded", "ok", output);
        } else {
          previewed = null;
          show("Preview failed", "fail", output);
        }
        return;
      }
      previewed = null;
      if (job.ok === false) {
        show("Apply failed", "fail", output);
        return;
      }
      show("Applied", "ok", output);
      if (job.restart_expected) {
        status.appendChild(h("p", { class: "warn" }, RESTART_MESSAGE));
      }
      if (onApplied) {
        onApplied(job);
      }
    } catch (err) {
      previewed = null;
      show(err.message, "error");
    } finally {
      busy = false;
      if (onBusy) {
        onBusy(false);
      }
      sync();
    }
  }

  preview.addEventListener("click", () => run(true));
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    if (!apply.disabled) {
      run(false);
    }
  });
  form.addEventListener("input", sync);
  sync();
  return { form, status };
}
