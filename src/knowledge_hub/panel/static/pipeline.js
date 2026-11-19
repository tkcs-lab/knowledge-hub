import { h, clear, fmt } from "/static/dom.js";
import { confirmButton, opForm, setConfigSha } from "/static/ops.js";
import { when, runOp } from "/static/groups.js";

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

function actionButton(label, notice, op, args, reload, ask) {
  const run = () => {
    busy += 1;
    runOp(notice, op, args, { sha: null, onApplied: reload }).finally(() => {
      busy -= 1;
    });
  };
  if (ask) {
    return confirmButton(label, run);
  }
  const button = h("button", { type: "button" }, label);
  button.addEventListener("click", run);
  return button;
}

function failedRow(job, notice, reload) {
  const actions = h("div", { class: "actions" });
  if (job.dead) {
    actions.appendChild(actionButton("Retry", notice, "jobs.retry", { ids: [job.id] }, reload, false));
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

function render(body, notice, data, reload) {
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
    const reclaim = opForm({ op: "jobs.reclaim", fields: [], previewless: true, onApplied: reload });
    reclaim.form.querySelector("button[type=submit]").textContent = "Reclaim stuck jobs";
    toolbar.appendChild(reclaim.form);
  }
  const requeue = opForm({ op: "embeddings.requeue", fields: [], previewless: true, onApplied: reload });
  requeue.form.querySelector("button[type=submit]").textContent = "Queue embeddings";
  toolbar.appendChild(requeue.form);
  body.appendChild(toolbar);

  body.appendChild(h("h3", {}, "Failed jobs"));
  if ((jobs.failed_dead || 0) > 0) {
    body.appendChild(actionButton("Retry dead jobs", notice, "jobs.retry", {}, reload, false));
  }
  body.appendChild(
    table(
      ["Id", "Kind", "Repo", "Attempts", "State", "Error", "Created", "Run after", "Actions"],
      (data.failed || []).map((j) => failedRow(j, notice, reload)),
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
          h("td", {}, actionButton(`Rebuild ${r.repo}`, notice, "repo.rebuild", { repo: r.repo }, reload, true))
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
  const notice = h("div", { class: "op-status", "aria-live": "polite" });
  const body = h("div", {});
  root.appendChild(notice);
  root.appendChild(body);
  const load = async (initial) => {
    try {
      const data = await window.api("GET", "/api/pipeline");
      if (data && data.config_sha256) {
        setConfigSha(data.config_sha256);
      }
      render(body, notice, data, () => load(false));
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
