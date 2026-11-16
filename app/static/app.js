"use strict";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const store = {
  get(k, d) { try { const v = localStorage.getItem("pd:" + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("pd:" + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};

let state = null;
let openPkgs = new Set();
let collapsed = new Set(store.get("collapsed", []));
let logSource = null;
let logJobId = null;
let pollTimer = null;

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Patchdeck": "1" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return r.json();
}

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (t.hidden = true), 3500);
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}
const clock = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function countPill(n) {
  if (n === 0) return `<span class="pill ok">Up to date</span>`;
  return `<span class="pill ${n >= 20 ? "bad" : "warn"}">${n} update${n === 1 ? "" : "s"}</span>`;
}

function jobPill(job) {
  if (!job) return "";
  const label = job.status === "queued" ? "Queued" : "Updating";
  return `<button class="pill info running" data-log="${esc(job.id)}">${label}</button>`;
}

function pkgList(pkgs) {
  return `<div class="pkgs">${pkgs.map((p) => `<span>${esc(p.name)}</span><span>${esc(p.from)} → ${esc(p.to)}</span>`).join("")}</div>`;
}

function windowLabel() {
  const m = state && state.maintenance;
  if (!m || !m.next_start) return null;
  const d = new Date(m.next_start * 1000);
  if (m.next_start * 1000 <= Date.now()) return "now";
  return d.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
}

/* Update / schedule buttons for one target (a container, or a host when vmid is null). */
function targetActions(hostName, vmid, t, n, updateLabel) {
  if (t.job) return "";
  const v = vmid == null ? "" : ` data-vmid="${vmid}"`;
  const h = esc(hostName);
  if (t.scheduled) {
    return `<button class="btn small sched" data-unschedule="${h}"${v} title="Scheduled for the maintenance window. Click to cancel.">🌙 ${esc(windowLabel() || "Scheduled")} <span aria-hidden="true">✕</span></button>`;
  }
  if (!n) return "";
  const win = windowLabel();
  const moonTitle = win ? `Schedule for the maintenance window (${win})` : "Set up a maintenance window in Settings to schedule updates";
  if (t.maint_only) {
    return `<button class="btn small primary" data-schedule="${h}"${v} title="${esc(moonTitle)}">🌙 Schedule</button>
            <button class="btn small ghost" data-update="${h}"${v} title="Update now instead of waiting for the window">Now</button>`;
  }
  return `<button class="btn small ${vmid == null ? "" : "primary"}" data-update="${h}"${v}>${updateLabel}</button>
          <button class="btn small icon" data-schedule="${h}"${v} title="${esc(moonTitle)}" aria-label="${esc(moonTitle)}">🌙</button>`;
}

function renderRow(host, c) {
  const key = `${host.name}/${c.vmid}`;
  const n = c.packages.length;
  const running = c.status === "running";
  let stateHtml;
  if (c.job) stateHtml = jobPill(c.job);
  else if (!running) stateHtml = `<span class="pill">${esc(c.status || "unknown")}</span>`;
  else if (c.error) stateHtml = `<span class="pill bad">Check failed</span><span class="err" title="${esc(c.error)}">${esc(c.error)}</span>`;
  else stateHtml = n ? `<button class="pill ${n >= 20 ? "bad" : "warn"}" data-pkgs="${esc(key)}" title="Show packages">${n} update${n === 1 ? "" : "s"} ▾</button>` : countPill(0);
  if (c.reboot) stateHtml += `<span class="pill bad">Reboot needed</span>`;
  if (c.warning) stateHtml += `<span class="pill warn" title="${esc(c.warning)}">!</span>`;

  return `
    <div class="row${running ? "" : " dim"}">
      <div class="ct-name"><strong>${esc(c.name)}</strong><span class="vmid">${c.vmid}</span></div>
      <div class="os">${esc(c.os || "")}</div>
      <div class="state">${stateHtml}</div>
      <div class="row-actions">
        ${running ? targetActions(host.name, c.vmid, c, n, "Update") : ""}
        ${c.console ? `<a class="btn small" href="${esc(c.console)}" target="_blank" rel="noopener">Console</a>` : ""}
      </div>
      ${openPkgs.has(key) && n ? pkgList(c.packages) : ""}
    </div>`;
}

function renderHost(h) {
  const hostPk = h.host ? h.host.packages : null;
  const n = hostPk ? hostPk.length : null;
  const key = `${h.name}/host`;
  let meta = "";
  if (h.checking) meta += `<span class="pill info running">Checking</span>`;
  if (h.job) meta += jobPill(h.job);
  else if (n !== null) meta += n ? `<button class="pill ${n >= 20 ? "bad" : "warn"}" data-pkgs="${esc(key)}">Host: ${n} update${n === 1 ? "" : "s"} ▾</button>` : `<span class="pill ok">Host up to date</span>`;
  if (h.host && h.host.reboot) meta += `<span class="pill bad">Reboot needed</span>`;
  if (h.error) meta += `<span class="pill bad" title="${esc(h.error)}">Unreachable</span><span class="err" title="${esc(h.error)}">${esc(h.error)}</span>`;

  const cts = h.containers || [];
  const isPve = h.kind === "pve";
  const rows = cts.length ? cts.map((c) => renderRow(h, c)).join("")
    : isPve ? `<div class="empty">${h.checked_at ? "No containers found." : "Not checked yet."}</div>` : "";
  const kindLabel = { pbs: "Backup Server", debian: "Server" }[h.kind];

  return `
    <section class="panel${collapsed.has(h.name) ? " collapsed" : ""}" data-host="${esc(h.name)}">
      <div class="host-head">
        <button class="chev" data-collapse="${esc(h.name)}" aria-label="Toggle ${esc(h.name)}">▼</button>
        <div class="host-title"><strong>${esc(h.name)}${kindLabel ? ` <span class="kind-tag">${kindLabel}</span>` : ""}</strong><span>${esc(h.address)} · checked ${ago(h.checked_at)}</span></div>
        <div class="host-meta">${meta}</div>
        <div class="host-actions">
          <button class="btn small" data-check="${esc(h.name)}" ${h.checking ? "disabled" : ""}>Check</button>
          ${targetActions(h.name, null, h, n, isPve ? "Update host" : "Update")}
          ${h.console ? `<a class="btn small" href="${esc(h.console)}" target="_blank" rel="noopener">Shell</a>` : ""}
        </div>
      </div>
      ${openPkgs.has(key) && n ? `<div class="row">${pkgList(hostPk)}</div>` : ""}
      ${rows ? `<div class="rows">${rows}</div>` : ""}
    </section>`;
}

function renderJobs(jobs) {
  if (!jobs.length) return `<li class="muted" style="cursor:default"><span></span><span></span><span>Nothing yet</span></li>`;
  const cls = { success: "ok", failed: "bad", running: "info running", queued: "info" };
  return jobs.slice(0, 12).map((j) => `
    <li data-log="${esc(j.id)}">
      <span class="when">${clock(j.created)}</span>
      <span class="kind muted">${j.kind === "check" ? "Check" : "Update"}</span>
      <span class="what">${esc(j.kind === "check" ? j.host : j.name)} ${j.summary ? `<em>· ${esc(j.summary)}</em>` : ""}</span>
      <span class="pill ${cls[j.status] || ""}">${esc(j.status)}</span>
    </li>`).join("");
}

function render() {
  if (!state) return;
  $("#hosts").innerHTML = state.hosts.length ? state.hosts.map(renderHost).join("")
    : `<section class="panel empty-state"><strong>No Proxmox hosts yet</strong><p class="muted">Add your hosts and set up SSH access in <a href="#settings">Settings</a>.</p></section>`;
  $("#jobs").innerHTML = renderJobs(state.jobs);

  let total = 0, targets = 0;
  for (const h of state.hosts) {
    const hn = h.host ? h.host.packages.length : 0;
    total += hn; if (hn) targets++;
    for (const c of h.containers) { total += c.packages.length; if (c.packages.length) targets++; }
  }
  const last = Math.max(0, ...state.hosts.map((h) => h.checked_at || 0));
  const next = state.next_check ? ` · next check ${clock(state.next_check)}` : "";
  const sched = state.maintenance && state.maintenance.scheduled ? ` · ${state.maintenance.scheduled} scheduled for ${windowLabel()}` : "";
  $("#summary").textContent = (total ? `${total} updates on ${targets} target${targets === 1 ? "" : "s"}` : "Everything up to date") + ` · checked ${ago(last || null)}${next}${sched}`;
  const ctPending = state.hosts.some((h) => h.containers.some((c) => c.packages.length && !c.job && c.status === "running"));
  $("#update-all").disabled = !ctPending;
  $("#check-all").disabled = state.hosts.some((h) => h.checking);
}

async function refresh() {
  try {
    state = await api("/api/state");
    render();
  } catch (e) {
    $("#summary").textContent = `Could not load state: ${e.message}`;
  }
  const busy = state && (state.jobs.some((j) => j.status === "running" || j.status === "queued"));
  clearTimeout(pollTimer);
  pollTimer = setTimeout(refresh, busy ? 2500 : 30000);
}

function openLog(jobId, title) {
  if (logSource) logSource.close();
  logJobId = jobId;
  const job = state && state.jobs.find((j) => j.id === jobId);
  $("#log-title").textContent = title || (job ? `${job.kind === "check" ? "Check" : "Update"} · ${job.kind === "check" ? job.host : job.name}` : "Job");
  const status = $("#log-status");
  const setStatus = (s) => { status.textContent = s; status.className = "pill " + ({ success: "ok", failed: "bad" }[s] || "info running"); };
  setStatus(job ? job.status : "running");
  const body = $("#log-body");
  body.textContent = "";
  $("#log").hidden = false;
  const src = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/stream`);
  logSource = src;
  src.addEventListener("line", (e) => {
    const stick = body.scrollTop + body.clientHeight >= body.scrollHeight - 30;
    body.textContent += JSON.parse(e.data) + "\n";
    if (stick) body.scrollTop = body.scrollHeight;
  });
  src.addEventListener("status", (e) => setStatus(JSON.parse(e.data)));
  src.addEventListener("end", (e) => { setStatus(JSON.parse(e.data)); src.close(); refresh(); });
  src.onerror = () => { if (src.readyState === EventSource.CLOSED) return; };
}

async function startUpdate(host, vmid) {
  const ct = vmid == null ? null : state.hosts.find((h) => h.name === host).containers.find((c) => c.vmid === vmid);
  if (vmid == null && !confirm(`Run apt dist-upgrade on ${host} now?`)) return;
  try {
    const job = await api("/api/update", { host, vmid, snapshot: $("#snapshot").checked });
    openLog(job.id, `Update · ${ct ? ct.name : host + " (host)"}`);
    refresh();
  } catch (e) { toast(e.message); }
}

document.addEventListener("click", async (e) => {
  const t = e.target.closest("button, li[data-log]");
  if (!t) return;
  if (t.dataset.collapse) {
    const n = t.dataset.collapse;
    collapsed.has(n) ? collapsed.delete(n) : collapsed.add(n);
    store.set("collapsed", [...collapsed]);
    render();
  } else if (t.dataset.pkgs) {
    const k = t.dataset.pkgs;
    openPkgs.has(k) ? openPkgs.delete(k) : openPkgs.add(k);
    render();
  } else if (t.dataset.schedule) {
    try {
      await api("/api/schedule", { host: t.dataset.schedule, vmid: t.dataset.vmid ? Number(t.dataset.vmid) : null, snapshot: $("#snapshot").checked });
      await refresh();
      toast(`Scheduled for the maintenance window (${windowLabel()})`);
    } catch (err) { toast(err.message); }
  } else if (t.dataset.unschedule) {
    try {
      await api("/api/schedule/cancel", { host: t.dataset.unschedule, vmid: t.dataset.vmid ? Number(t.dataset.vmid) : null });
      toast("Removed from the maintenance window");
      refresh();
    } catch (err) { toast(err.message); }
  } else if (t.dataset.update) {
    startUpdate(t.dataset.update, t.dataset.vmid ? Number(t.dataset.vmid) : null);
  } else if (t.dataset.check) {
    try { const r = await api("/api/check", { host: t.dataset.check }); openLog(r.jobs[0].id, `Check · ${t.dataset.check}`); refresh(); }
    catch (err) { toast(err.message); }
  } else if (t.dataset.log) {
    openLog(t.dataset.log);
  }
});

$("#check-all").addEventListener("click", async () => {
  try { await api("/api/check", {}); toast("Checking all hosts…"); refresh(); } catch (e) { toast(e.message); }
});

$("#update-all").addEventListener("click", async () => {
  const snap = $("#snapshot").checked;
  const cts = state.hosts.flatMap((h) => h.containers).filter((c) => c.packages.length && !c.job && !c.scheduled && c.status === "running");
  const later = windowLabel() ? cts.filter((c) => c.maint_only).length : 0;
  const msg = `Update ${cts.length - later} container${cts.length - later === 1 ? "" : "s"} now${snap ? " (snapshot first)" : ""}?`
    + (later ? `\n${later} marked "maintenance window only" will be scheduled for ${windowLabel()} instead.` : "")
    + "\nHosts are not included.";
  if (!confirm(msg)) return;
  try {
    const r = await api("/api/update-all", { snapshot: snap });
    const parts = [];
    if (r.jobs.length) parts.push(`Started ${r.jobs.length} update${r.jobs.length === 1 ? "" : "s"}`);
    if (r.scheduled) parts.push(`${r.scheduled} scheduled for the maintenance window`);
    toast(parts.join(" · ") || "Nothing to update");
    if (r.jobs.length) openLog(r.jobs[0].id);
    refresh();
  } catch (e) { toast(e.message); }
});

$("#log-close").addEventListener("click", () => {
  if (logSource) logSource.close();
  $("#log").hidden = true;
  logJobId = null;
});

$("#snapshot").addEventListener("change", (e) => store.set("snapshot", e.target.checked));

(async function init() {
  await refresh();
  $("#snapshot").checked = store.get("snapshot", state ? state.snapshot_default : true);
})();
