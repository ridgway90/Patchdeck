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

const fmtWhen = (ts) => new Date(ts * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
const fmtTime = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

function windowText(w) {
  if (!w || !w.start) return w ? w.name : "";
  const open = w.start * 1000 <= Date.now();
  return open ? `${w.name} (open now, until ${fmtTime(w.end)})` : `${w.name}: ${fmtWhen(w.start)} – ${fmtTime(w.end)}`;
}

function findTarget(hostName, vmid, stack) {
  const h = state.hosts.find((x) => x.name === hostName);
  if (!h) return null;
  const t = vmid == null ? h : h.containers.find((c) => c.vmid === vmid);
  if (!stack || !t) return t;
  const stacks = (vmid == null ? (t.host || {}).stacks : t.stacks) || [];
  return stacks.find((x) => x.name === stack) || null;
}

/* Buttons for one target (a guest, or a host when vmid is null). */
function targetActions(hostName, vmid, t, n, label, stack) {
  if (t.job) return "";
  const v = (vmid == null ? "" : ` data-vmid="${vmid}"`) + (stack ? ` data-stack="${esc(stack)}"` : "");
  const h = esc(hostName);
  if (t.scheduled) {
    const w = t.scheduled.window;
    const when = w && w.start ? (w.start * 1000 <= Date.now() ? "now" : fmtWhen(w.start)) : "scheduled";
    return `<button class="btn small sched" data-dialog="${h}"${v} title="Scheduled for ${esc(windowText(w))}${t.scheduled.reboot ? ", with reboot if needed" : ""}. Click to change.">🌙 ${esc(when)}${t.scheduled.reboot ? " ↻" : ""}</button>`;
  }
  if (!n) return "";
  return `<button class="btn small ${vmid == null && !stack ? "" : "primary"}" data-dialog="${h}"${v}>${label}</button>`;
}

const IMG_STATUS = { update: ["warn", "New version"], current: ["ok", "Current"], local: ["", "Local build"], unknown: ["", "Can't check"] };

function stackRows(host, vmid, stacks, dockerError) {
  let out = "";
  if (dockerError) out += `<div class="row stack-row"><div class="ct-name"><span class="whale" aria-hidden="true">🐳</span><span class="err">${esc(dockerError)}</span></div></div>`;
  for (const st of stacks || []) {
    const key = `${host.name}/${vmid}/stack/${st.name}`;
    const n = st.updates || 0;
    const imgs = st.images || [];
    let stateHtml;
    if (st.job) stateHtml = jobPill(st.job);
    else if (!imgs.length) stateHtml = `<span class="pill">No images</span>`;
    else stateHtml = `<button class="pill ${n ? "warn" : "ok"}" data-pkgs="${esc(key)}" title="Show images">${n ? `${n} image update${n === 1 ? "" : "s"}` : "Images current"} ▾</button>`;
    const unknown = imgs.filter((i) => i.status === "unknown").length;
    if (unknown && !st.job) stateHtml += `<span class="pill" title="${esc(imgs.filter((i) => i.status === "unknown").map((i) => `${i.ref}: ${i.note}`).join("\n"))}">${unknown} unchecked</span>`;
    stateHtml += scheduleTag(st);
    out += `
      <div class="row stack-row">
        <div class="ct-name"><span class="whale" aria-hidden="true">🐳</span><strong>${esc(st.name)}</strong></div>
        <div class="os">${esc(st.status || "")}</div>
        <div class="state">${stateHtml}</div>
        <div class="row-actions">${targetActions(host.name, vmid, st, n, "Update…", st.name)}</div>
        ${openPkgs.has(key) ? `<div class="pkgs imgs">${imgs.map((i) => {
          const [cls, label] = IMG_STATUS[i.status] || ["", i.status];
          return `<span>${esc(i.ref)}</span><span class="${cls}-text" title="${esc(i.note || "")}">${label}${i.note ? ` · ${esc(i.note)}` : ""}</span>`;
        }).join("")}</div>` : ""}
      </div>`;
  }
  return out;
}

function scheduleTag(t) {
  if (!t.schedule) return "";
  const p = t.schedule;
  return `<span class="pill sched-tag" title="Maintenance schedule: ${esc(p.window)}${p.auto ? ", updates automatically" : ""}${p.reboot ? ", reboots if needed" : ""}">🌙 ${esc(p.window)}${p.auto ? " · auto" : ""}</span>`;
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
  stateHtml += scheduleTag(c);

  return `
    <div class="row${running ? "" : " dim"}">
      <div class="ct-name"><strong>${esc(c.name)}</strong><span class="vmid">${c.vmid}</span>${c.type === "vm" ? `<span class="kind-tag">VM</span>` : ""}</div>
      <div class="os">${esc(({ mswindows: "Windows" })[c.os] || c.os || "")}</div>
      <div class="state">${stateHtml}</div>
      <div class="row-actions">
        ${running ? targetActions(host.name, c.vmid, c, n, "Update…") : ""}
        ${c.console ? `<a class="btn small" href="${esc(c.console)}" target="_blank" rel="noopener">Console</a>` : ""}
      </div>
      ${openPkgs.has(key) && n ? pkgList(c.packages) : ""}
    </div>${running ? stackRows(host, c.vmid, c.stacks, c.docker_error) : ""}`;
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
  const hostStacks = h.host ? stackRows(h, null, h.host.stacks, h.host.docker_error) : "";
  const rows = (cts.length ? cts.map((c) => renderRow(h, c)).join("")
    : isPve ? `<div class="empty">${h.checked_at ? "No containers found." : "Not checked yet."}</div>` : "") + hostStacks;
  const kindLabel = { pbs: "Backup Server", debian: "Server" }[h.kind];

  return `
    <section class="panel${collapsed.has(h.name) ? " collapsed" : ""}" data-host="${esc(h.name)}">
      <div class="host-head">
        <button class="chev" data-collapse="${esc(h.name)}" aria-label="Toggle ${esc(h.name)}">▼</button>
        <div class="host-title"><strong>${esc(h.name)}${kindLabel ? ` <span class="kind-tag">${kindLabel}</span>` : ""}</strong><span>${esc(h.address)} · checked ${ago(h.checked_at)}</span></div>
        <div class="host-meta">${meta}</div>
        <div class="host-actions">
          <button class="btn small" data-check="${esc(h.name)}" ${h.checking ? "disabled" : ""}>Check</button>
          ${scheduleTag(h)}
          ${targetActions(h.name, null, h, n, isPve ? "Update host…" : "Update…")}
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

function allStacks() {
  const out = [];
  for (const h of state.hosts) {
    for (const st of (h.host && h.host.stacks) || []) out.push({ host: h.name, vmid: null, st });
    for (const c of h.containers) if (c.status === "running") for (const st of c.stacks || []) out.push({ host: h.name, vmid: c.vmid, st });
  }
  return out;
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
  const stacksPending = allStacks().filter((x) => x.st.updates && !x.st.job && !x.st.scheduled);
  const sched = (stacksPending.length ? ` · ${stacksPending.length} Docker stack${stacksPending.length === 1 ? "" : "s"} with updates` : "")
    + (state.scheduled_count ? ` · ${state.scheduled_count} scheduled` : "");
  $("#summary").textContent = (total ? `${total} updates on ${targets} target${targets === 1 ? "" : "s"}` : "Everything up to date") + ` · checked ${ago(last || null)}${next}${sched}`;
  const ctPending = stacksPending.length || state.hosts.some((h) => h.containers.some((c) => c.packages.length && !c.job && c.status === "running"));
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

/* ---------- update dialog ---------- */

let dlg = null;  // { host, vmid, all, stack }

function openUpdateDialog(host, vmid, all = false, stack = null) {
  const t = all ? null : findTarget(host, vmid, stack);
  if (!all && !t) return;
  dlg = { host, vmid, all, stack };
  const wins = state.windows || [];
  const isHost = !all && vmid == null && !stack;
  const hostObj = isHost ? t : null;
  const policy = t && t.schedule;
  const sched = t && t.scheduled;

  let title, sub;
  if (all) {
    const pend = state.hosts.flatMap((h) => h.containers).filter((c) => c.packages.length && !c.job && !c.scheduled && c.status === "running");
    const withPolicy = pend.filter((c) => c.schedule).length;
    title = "Update all guests";
    sub = `${pend.length} container${pend.length === 1 ? "" : "s"}/VM${pend.length === 1 ? "" : "s"} with updates. Hosts are updated separately.`;
    $("#upd-override-row").hidden = !withPolicy;
    $("#upd-override-text").textContent = `Also update the ${withPolicy} with their own maintenance schedule now (otherwise they're queued for their window)`;
    $("#upd-override").checked = false;
    const stk = allStacks().filter((x) => x.st.updates && !x.st.job && !x.st.scheduled && x.vmid != null);
    $("#upd-docker-row").hidden = !stk.length;
    $("#upd-docker-text").textContent = `Also update ${stk.length} Docker stack${stk.length === 1 ? "" : "s"} with new images`;
    $("#upd-docker").checked = false;
  } else if (stack) {
    const guest = findTarget(host, vmid);
    const where = vmid == null ? host : `${guest.type === "vm" ? "VM" : "container"} ${guest.name} (${vmid}) on ${host}`;
    title = `Update stack ${stack}`;
    sub = `${t.updates} of ${t.images.length} image${t.images.length === 1 ? "" : "s"} have new versions · ${where}. Runs docker compose pull && up -d.`;
    $("#upd-override-row").hidden = true;
    $("#upd-docker-row").hidden = true;
  } else {
    const n = isHost ? (t.host ? t.host.packages.length : 0) : t.packages.length;
    title = isHost ? `Update ${host}` : `Update ${t.name}`;
    sub = `${n} package${n === 1 ? "" : "s"} to update` + (isHost ? ` on the ${({ pve: "Proxmox host", pbs: "Backup Server" })[hostObj.kind] || "server"}` : ` · ${t.type === "vm" ? "VM" : "container"} ${vmid} on ${host}`);
    $("#upd-override-row").hidden = true;
    $("#upd-docker-row").hidden = true;
  }
  $("#upd-title").textContent = title;
  $("#upd-sub").textContent = sub;

  const sel = $("#upd-window");
  sel.innerHTML = wins.map((w) => `<option value="${esc(w.name)}">${esc(windowText(w))}</option>`).join("");
  const wanted = (sched && sched.window && sched.window.name) || (policy && policy.window) || (wins[0] && wins[0].name);
  if (wanted) sel.value = wanted;
  const radios = document.querySelectorAll('#upd-form input[name="when"]');
  radios[1].disabled = !wins.length;
  sel.disabled = !wins.length;
  $("#upd-win-hint").textContent = wins.length ? (policy ? `This ${isHost ? "host" : "guest"} normally uses "${policy.window}".` : "") : "Add a maintenance window in Settings to use this.";
  $("#upd-now-hint").textContent = all ? "" : "Runs straight away and shows the live log.";
  const when = sched || (policy && wins.length) ? "schedule" : "now";
  radios.forEach((r) => (r.checked = r.value === when));

  $("#upd-snap-row").hidden = isHost || (stack && vmid == null);
  $("#upd-snap-text").textContent = stack ? `Snapshot the ${findTarget(host, vmid).type === "vm" ? "VM" : "container"} first` : "Take a snapshot first";
  $("#upd-reboot-row").hidden = !!stack;
  $("#upd-snap").checked = sched ? sched.snapshot : store.get("snapshot", state.snapshot_default);
  $("#upd-reboot").checked = sched ? sched.reboot : policy ? policy.reboot : store.get("reboot", false);
  $("#upd-unschedule").hidden = !sched;
  syncDialog();
  $("#upd").showModal();
}

function syncDialog() {
  if (!dlg) return;
  const when = document.querySelector('#upd-form input[name="when"]:checked').value;
  $("#upd-go").textContent = when === "now" ? "Update now" : (dlg.all ? "Schedule all" : "Schedule");
  $("#upd-window").closest(".choice").classList.toggle("on", when === "schedule");
  const isHost = !dlg.all && dlg.vmid == null && !dlg.stack;
  const warn = $("#upd-warn");
  if (isHost && $("#upd-reboot").checked) {
    const h = findTarget(dlg.host, null);
    warn.textContent = h.kind === "pve"
      ? "Rebooting a Proxmox host restarts every guest on it (1 minute after the update). If Patchdeck runs on this host it will be offline for a few minutes too."
      : "The server reboots 1 minute after the update. Anything running on it (e.g. backups on a PBS) will be interrupted.";
    warn.hidden = false;
  } else warn.hidden = true;
}

$("#upd-form").addEventListener("change", syncDialog);
$("#upd-cancel").addEventListener("click", () => $("#upd").close());

$("#upd-unschedule").addEventListener("click", async () => {
  try {
    await api("/api/schedule/cancel", { host: dlg.host, vmid: dlg.vmid, stack: dlg.stack });
    $("#upd").close();
    toast("Removed from the maintenance window");
    refresh();
  } catch (e) { toast(e.message); }
});

$("#upd-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const when = document.querySelector('#upd-form input[name="when"]:checked').value;
  const snapshot = !$("#upd-snap-row").hidden && $("#upd-snap").checked;
  const reboot = !dlg.stack && $("#upd-reboot").checked;
  const window = $("#upd-window").value;
  if (!$("#upd-snap-row").hidden) store.set("snapshot", snapshot);
  store.set("reboot", reboot);
  const go = $("#upd-go");
  go.disabled = true;
  try {
    if (dlg.all) {
      const r = await api("/api/update-all", { mode: when, window, snapshot, reboot, override: $("#upd-override").checked,
        include_docker: !$("#upd-docker-row").hidden && $("#upd-docker").checked });
      const parts = [];
      if (r.jobs.length) parts.push(`Started ${r.jobs.length} update${r.jobs.length === 1 ? "" : "s"}`);
      if (r.scheduled) parts.push(`${r.scheduled} scheduled`);
      toast(parts.join(" · ") || "Nothing to update");
      $("#upd").close();
      if (r.jobs.length) openLog(r.jobs[0].id);
    } else if (when === "now") {
      const job = await api("/api/update", { host: dlg.host, vmid: dlg.vmid, snapshot, reboot, stack: dlg.stack });
      $("#upd").close();
      openLog(job.id, `Update · ${job.name}`);
    } else {
      await api("/api/schedule", { host: dlg.host, vmid: dlg.vmid, snapshot, reboot, window, stack: dlg.stack });
      $("#upd").close();
      toast(`Scheduled: ${$("#upd-window").selectedOptions[0].textContent}`);
    }
    refresh();
  } catch (err) { toast(err.message); }
  finally { go.disabled = false; }
});

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
  } else if (t.dataset.dialog) {
    openUpdateDialog(t.dataset.dialog, t.dataset.vmid ? Number(t.dataset.vmid) : null, false, t.dataset.stack || null);
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

$("#update-all").addEventListener("click", () => openUpdateDialog(null, null, true));

$("#log-close").addEventListener("click", () => {
  if (logSource) logSource.close();
  $("#log").hidden = true;
  logJobId = null;
});


(async function init() {
  await refresh();
})();
