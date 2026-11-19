export async function api(method, path, body) {
  const init = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body === undefined ? {} : body);
  }
  const resp = await fetch(path, init);
  if (resp.status === 401) {
    window.location.href = "/auth/login";
    throw new Error("unauthorized");
  }
  let data = null;
  if (resp.status !== 204) {
    try {
      data = await resp.json();
    } catch (err) {
      data = null;
    }
  }
  if (!resp.ok) {
    const detail = data && (data.detail || data.error);
    throw new Error(detail || "request failed (" + resp.status + ")");
  }
  return data;
}

window.api = api;
window.panelTabs = window.panelTabs || {};

const TABS = ["groups", "labels", "repos", "pipeline", "health"];
const main = document.querySelector("main");
const buttons = Array.from(document.querySelectorAll("nav button[data-tab]"));
const sections = {};

for (const name of TABS) {
  const section = document.createElement("section");
  section.id = "tab-" + name;
  section.hidden = true;
  main.appendChild(section);
  sections[name] = section;
}

function show(name) {
  for (const tab of TABS) {
    sections[tab].hidden = tab !== name;
  }
  for (const button of buttons) {
    button.setAttribute("aria-selected", button.dataset.tab === name ? "true" : "false");
  }
  const open = window.panelTabs[name];
  if (open) {
    open(sections[name]);
  }
}

for (const button of buttons) {
  button.addEventListener("click", () => show(button.dataset.tab));
}

document.getElementById("signout").addEventListener("click", async () => {
  try {
    await api("POST", "/api/logout", {});
  } finally {
    window.location.href = "/auth/login";
  }
});

api("GET", "/api/me")
  .then((me) => {
    document.getElementById("me").textContent = me.email;
  })
  .catch(() => {});

show(TABS[0]);
