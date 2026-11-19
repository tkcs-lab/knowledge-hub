import { h, clear, fmt, when } from "/static/dom.js";
import { opForm, setConfigSha } from "/static/ops.js";

const REFRESH_MS = 15000;
let timer = null;
let busy = 0;

function age(seconds) {
  if (seconds === null || seconds === undefined) {
    return "none queued";
  }
  if (seconds < 120) {
    return `${Math.round(seconds)} s`;
  }
  if (seconds < 7200) {
    return `${Math.round(seconds / 60)} min`;
  }
  return `${(seconds / 3600).toFixed(1)} h`;
}

function counter(label, value, cls) {
  return h("div", { class: "counter " + (cls || "") }, h("span", { class: "counter-value" }, fmt(value)), h("span", { class: "counter-label" }, label));
}

// These ops have no dry run, so each is a one-click form (previewless); confirmText asks first.
function action(op, fixed, label, reload, confirmText) {
  return opForm({
    op,
    fields: [],
    fixed,
    previewless: true,
    submitLabel: label,
    confirmText,
    onApplied: reload,
    onBusy: (on) => {
      busy += on ? 1 : -1;
    },
  }).form;
}

function failedRow(job, reload) {
  const actions = h("div", { class: "actions" });
  if (job.dead) {
    actions.appendChild(action("jobs.retry", { ids: [job.id] }, "Retry", reload, null));
  }
  return h(
    "tr",
    {},
    h("td", {}, job.id),
    h("td", {}, job.kind),
    h("td", {}, job.repo || ""),
    h("td", {}, job.attempts),
    h("td", { class: job.dead ? "fail" : "warn" }, job.dead ? "dead" : "retrying"),
    h("td", { class: "error-text" }, job.last_error || ""),
    h("td", {}, when(job.created_at)),
    h("td", {}, when(job.run_after)),
    h("td", {}, actions)
  );
}

function table(head, rows, empty) {
  if (rows.length === 0) {
    return h("p", { class: "muted" }, empty);
  }
  return h("table", {}, h("thead", {}, h("tr", {}, head.map((t) => h("th", {}, t)))), h("tbody", {}, rows));
}

function render(body, data, reload) {
  clear(body);
  const jobs = data.jobs || {};
  body.appendChild(h("h2", {}, "Pipeline"));
  body.appendChild(
    h(
      "div",
      { class: "counters" },
      counter("Queued", jobs.queued),
      counter("Running", jobs.running),
      counter("Done", jobs.done),
      counter("Failed, retrying", jobs.failed_retrying, jobs.failed_retrying > 0 ? "warn" : ""),
      counter("Failed, dead", jobs.failed_dead, jobs.failed_dead > 0 ? "fail" : "")
    )
  );
  body.appendChild(
    h(
      "p",
      {},
      `Oldest queued job: ${age(data.oldest_queued_age_s)}. Stuck running jobs: ${fmt(data.stuck_running)}. Records without chunks: ${fmt(data.records_without_chunks)}.`
    )
  );

  const toolbar = h("div", { class: "actions" });
  if (data.stuck_running > 0) {
    toolbar.appendChild(action("jobs.reclaim", null, "Reclaim stuck jobs", reload, null));
  }
  toolbar.appendChild(action("embeddings.requeue", null, "Queue embeddings", reload, null));
  body.appendChild(toolbar);

  body.appendChild(h("h3", {}, "Failed jobs"));
  if ((jobs.failed_dead || 0) > 0) {
    body.appendChild(action("jobs.retry", {}, "Retry dead jobs", reload, null));
  }
  body.appendChild(
    table(
      ["Id", "Kind", "Repo", "Attempts", "State", "Error", "Created", "Run after", "Actions"],
      (data.failed || []).map((j) => failedRow(j, reload)),
      "No failed jobs."
    )
  );

  body.appendChild(h("h3", {}, "Repos"));
  body.appendChild(
    table(
      ["Repo", "Last indexed sha", "Indexed", "Actions"],
      (data.repos || []).map((r) => {
        return h(
          "tr",
          {},
          h("td", {}, r.repo),
          h("td", {}, r.last_indexed_sha ? r.last_indexed_sha.slice(0, 8) : ""),
          h("td", {}, r.indexed_at ? when(r.indexed_at) : ""),
          h("td", {}, action("repo.rebuild", { repo: r.repo }, `Rebuild ${r.repo}`, reload, `Rebuild ${r.repo}?`))
        );
      }),
      "No repos indexed yet."
    )
  );

  body.appendChild(h("h3", {}, "Alerts"));
  body.appendChild(
    table(
      ["Id", "Repo", "Path", "Message", "Created"],
      (data.alerts || []).map((a) =>
        h("tr", {}, h("td", {}, a.id), h("td", {}, a.repo || ""), h("td", {}, a.path || ""), h("td", {}, a.message || ""), h("td", {}, when(a.created_at)))
      ),
      "No alerts."
    )
  );
}

window.panelTabs = window.panelTabs || {};
window.panelTabs.pipeline = (root) => {
  clear(root);
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
  const body = h("div", {});
  root.appendChild(body);
  const load = async (initial) => {
    try {
      const data = await window.api("GET", "/api/pipeline");
      if (data && data.config_sha256) {
        setConfigSha(data.config_sha256);
      }
      render(body, data, () => load(false));
    } catch (err) {
      if (initial) {
        clear(body);
      }
      body.appendChild(h("p", { class: "error" }, err.message));
    }
  };
  load(true);
  timer = setInterval(() => {
    if (!root.isConnected || root.hidden) {
      clearInterval(timer);
      timer = null;
      return;
    }
    if (busy === 0) {
      load(false);
    }
  }, REFRESH_MS);
};
