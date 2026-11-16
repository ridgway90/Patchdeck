"use strict";
/* Settings view: hosts + SSH setup, schedule, Discord, snapshots. Uses $, esc, api, toast from app.js. */

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let S = null;            // last settings payload
let schedDraft = null;   // unsaved schedule edits
const fp = {};           // fetched fingerprints by host name
const tested = {};       // last connection test by host name
const editing = new Set();
const expanded = new Set();   // ready hosts whose setup steps are shown

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(() => toast("Copied"));
  }
  // Clipboard API needs HTTPS; fall back for plain-HTTP LAN access.
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); toast("Copied"); } catch { toast("Copy failed, select the text manually"); }
  ta.remove();
  return Promise.resolve();
}

function busy(btn, on, label) {
  if (!btn) return;
  if (on) { btn.dataset.label = btn.textContent; btn.textContent = label || "Working…"; btn.disabled = true; }
  else { btn.textContent = btn.dataset.label || btn.textContent; btn.disabled = false; }
}

async function act(btn, path, body, label) {
  busy(btn, true, label);
  try { return await api(path, body); }
  catch (e) { toast(e.message); return null; }
  finally { busy(btn, false); }
}

/* ---------- sections ---------- */

function keySection() {
  const k = S.key;
  if (!k.public) {
    return `
      <p>Patchdeck needs its own SSH key to log in to your Proxmox hosts. Generate one here; the private key never leaves this server.</p>
      <div class="form-actions"><button class="btn primary" data-act="gen-key">Generate SSH key</button></div>`;
  }
  return `
    <p>This is Patchdeck's public key. Use <strong>Install key</strong> on each host below, or add it to <code>/root/.ssh/authorized_keys</code> yourself.</p>
    <div class="code"><code>${esc(k.public)}</code><button class="btn small" data-copy="${esc(k.public)}">Copy</button></div>
    <p class="hint">Private key: <code>${esc(k.path)}</code></p>`;
}

function hostCard(h) {
  const f = fp[h.name];
  const t = tested[h.name];
  const steps = [];

  if (editing.has(h.name)) return hostForm(h);

  // Step 1: fingerprint
  let s1;
  if (!S.host_key_checking) s1 = `<span class="muted">Host key checking is disabled in config.yaml.</span>`;
  else if (f) {
    s1 = `
      <div class="fp">
        <div><span class="muted">${esc(f.type)}</span> <code>${esc(f.fingerprint)}</code></div>
        <p class="hint">Check it matches by running this in the host's shell (Proxmox UI → ${esc(h.name)} → Shell):</p>
        <div class="code"><code>${esc(f.verify_cmd)}</code><button class="btn small" data-copy="${esc(f.verify_cmd)}">Copy</button></div>
        <div class="form-actions">
          <button class="btn small primary" data-act="trust" data-host="${esc(h.name)}">It matches, trust it</button>
          <button class="btn small ghost" data-act="fp-cancel" data-host="${esc(h.name)}">Cancel</button>
        </div>
      </div>`;
  } else {
    s1 = `<button class="btn small" data-act="fp" data-host="${esc(h.name)}">${h.trusted ? "Re-check fingerprint" : "Fetch fingerprint"}</button>`;
  }
  steps.push(step(1, "Trust the host's fingerprint", h.trusted, s1));

  // Step 2: authorise key
  let s2;
  if (!S.key.public) s2 = `<span class="muted">Generate Patchdeck's SSH key first (above).</span>`;
  else s2 = `
      <form class="inline" data-form="install" data-host="${esc(h.name)}">
        <input type="password" name="password" placeholder="${esc(h.user)} password on ${esc(h.name)}" autocomplete="off" ${h.trusted ? "" : "disabled"} required>
        <button class="btn small" ${h.trusted ? "" : "disabled"}>Install key</button>
      </form>
      <p class="hint">The password is used once to add the key and is not stored.
        <button type="button" class="linkish" data-act="toggle-manual" data-host="${esc(h.name)}">Or add it manually</button></p>
      <div class="manual" data-manual="${esc(h.name)}" hidden>
        <p class="hint">Paste this into the host's shell:</p>
        <div class="code"><code>${esc(S.key.authorize_cmd)}</code><button class="btn small" data-copy="${esc(S.key.authorize_cmd)}">Copy</button></div>
      </div>`;
  const ok = t ? t.ok : h.connected;
  steps.push(step(2, "Authorise Patchdeck's key", ok, s2));

  // Step 3: test
  let result = "";
  if (t) result = t.ok
    ? `<span class="ok-text">✓ Connected to <strong>${esc(t.node)}</strong> · ${esc(t.version)}${t.containers != null ? ` · ${t.containers} containers` : ""}</span>`
    : `<span class="bad-text">✗ ${esc(t.error)}</span>`;
  steps.push(step(3, "Test the connection", ok,
    `<div class="inline"><button class="btn small" data-act="test" data-host="${esc(h.name)}">Test connection</button>${result}</div>`));

  const status = ok ? `<span class="pill ok">Ready</span>`
    : h.trusted ? `<span class="pill warn">Fingerprint trusted</span>` : `<span class="pill">Not set up</span>`;

  return `
    <div class="host-card">
      <div class="host-card-head">
        <div class="host-title"><strong>${esc(h.name)}${h.type !== "pve" ? ` <span class="kind-tag">${esc(S.host_kinds[h.type])}</span>` : ""}</strong><span>${esc(h.user)}@${esc(h.address)}${h.port !== 22 ? ":" + h.port : ""}</span></div>
        ${status}
        <div class="host-actions">
          ${ok ? `<button class="btn small ghost" data-act="expand" data-host="${esc(h.name)}">${expanded.has(h.name) ? "Hide setup" : "Setup & test"}</button>` : ""}
          <button class="btn small ghost" data-act="edit" data-host="${esc(h.name)}">Edit</button>
          <button class="btn small ghost danger" data-act="remove" data-host="${esc(h.name)}">Remove</button>
        </div>
      </div>
      ${ok && !expanded.has(h.name) ? (t && t.ok ? `<div class="ready-line">${result}</div>` : "") : `<ol class="steps">${steps.join("")}</ol>`}
    </div>`;
}

function step(n, title, done, body) {
  return `<li class="${done ? "done" : ""}"><span class="num">${done ? "✓" : n}</span><div><strong>${title}</strong><div class="step-body">${body}</div></div></li>`;
}

function hostForm(h) {
  const isEdit = !!h;
  return `
    <form class="host-card host-form" data-form="host" ${isEdit ? `data-original="${esc(h.name)}"` : ""}>
      <div class="fields">
        <label class="field"><span>Name</span><input name="name" value="${esc(h ? h.name : "")}" placeholder="pve1" required></label>
        <label class="field grow"><span>IP address or hostname</span><input name="address" value="${esc(h ? h.address : "")}" placeholder="192.168.1.10" required></label>
        <label class="field narrow"><span>SSH port</span><input name="port" type="number" min="1" max="65535" value="${h ? h.port : 22}"></label>
        <label class="field"><span>Type</span>
          <select name="type">${Object.entries(S.host_kinds).map(([k, label]) =>
            `<option value="${k}" ${(h ? h.type : "pve") === k ? "selected" : ""}>${esc(label)}</option>`).join("")}</select>
        </label>
      </div>
      <div class="form-actions">
        <button class="btn primary small">${isEdit ? "Save" : "Add host"}</button>
        ${isEdit ? `<button type="button" class="btn small ghost" data-act="edit-cancel" data-host="${esc(h.name)}">Cancel</button>` : ""}
      </div>
    </form>`;
}

function hostsSection() {
  const cards = S.hosts.map(hostCard).join("");
  return `
    ${cards || `<p class="muted">No hosts yet. Add your first Proxmox host below.</p>`}
    <h3>Add a host</h3>
    ${hostForm(null)}`;
}

function scheduleSection() {
  const d = schedDraft;
  const next = S.schedule.next ? new Date(S.schedule.next * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "none scheduled";
  return `
    <p>Patchdeck checks every host and container for updates at these times, then sends the Discord summary.</p>
    <div class="field"><span>Days</span>
      <div class="days">${DAYS.map((name, i) => `<button type="button" class="day ${d.days.includes(i) ? "on" : ""}" data-day="${i}" aria-pressed="${d.days.includes(i)}">${name}</button>`).join("")}</div>
    </div>
    <div class="field"><span>Times</span>
      <div class="times">
        ${d.check_times.map((t, i) => `<span class="time"><input type="time" value="${esc(t)}" data-time="${i}" required><button type="button" class="x" data-del-time="${i}" aria-label="Remove ${esc(t)}">✕</button></span>`).join("")}
        <button type="button" class="btn small" data-act="add-time">+ Add time</button>
      </div>
    </div>
    <p class="hint">Next check: <strong>${esc(next)}</strong> (server time). Updates are never installed automatically.</p>
    <div class="form-actions"><button class="btn primary small" data-act="save-schedule">Save schedule</button></div>`;
}

let winDraft = null;  // unsaved maintenance window edits

function windowsSection() {
  if (!winDraft) winDraft = S.windows.map((w) => ({ was: w.name, name: w.name, days: [...w.days], start: w.start, end: w.end }));
  const fmt = (w) => w.next_start ? `${new Date(w.next_start * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" })}` : "never (no days selected)";
  const next = Object.fromEntries(S.windows.map((w) => [w.name, fmt(w)]));
  const cards = winDraft.map((w, i) => `
    <div class="win-card" data-win="${i}">
      <div class="fields">
        <label class="field grow"><span>Name</span><input data-wf="name" value="${esc(w.name)}" placeholder="e.g. Weekend nights" required></label>
        <label class="field narrow"><span>Starts</span><input type="time" data-wf="start" value="${esc(w.start)}" required></label>
        <label class="field narrow"><span>Ends</span><input type="time" data-wf="end" value="${esc(w.end)}" required></label>
        <button type="button" class="btn small ghost danger" data-act="win-del" data-i="${i}">Remove</button>
      </div>
      <div class="days">${DAYS.map((d, j) => `<button type="button" class="day wday ${w.days.includes(j) ? "on" : ""}" data-wday="${j}" aria-pressed="${w.days.includes(j)}">${d}</button>`).join("")}</div>
      ${w.was && next[w.was] ? `<p class="hint">Next: <strong>${esc(next[w.was])}</strong></p>` : ""}
    </div>`).join("");
  return `
    <p>Windows are the times updates are allowed to run out of hours. Give guests a window below, or pick one when you click Update.
      Updates in a window run one at a time per host; anything not started by the end time waits for the next one. An end time before the start runs past midnight.</p>
    <div class="windows">${cards || `<p class="muted">No windows yet.</p>`}</div>
    <div class="form-actions">
      <button type="button" class="btn small" data-act="win-add">+ Add window</button>
      <button type="button" class="btn small primary" data-act="win-save">Save windows</button>
    </div>`;
}

function collectWindows() {
  document.querySelectorAll("#s-windows .win-card").forEach((card) => {
    const w = winDraft[Number(card.dataset.win)];
    card.querySelectorAll("[data-wf]").forEach((inp) => (w[inp.dataset.wf] = inp.value.trim()));
    w.days = [...card.querySelectorAll(".wday.on")].map((x) => Number(x.dataset.wday));
  });
}

function schedulesSection() {
  if (!S.windows.length) return `<p class="muted">Add and save a maintenance window first.</p>`;
  const opts = (cur) => `<option value="">No schedule</option>` + S.windows.map((w) =>
    `<option value="${esc(w.name)}" ${cur === w.name ? "selected" : ""}>${esc(w.name)}</option>`).join("");
  const row = (key, label, isStack) => {
    const p = S.schedules[key] || {};
    return `<tr data-key="${esc(key)}">
      <td>${label}</td>
      <td><select data-sf="window" aria-label="Window for ${esc(key)}">${opts(p.window)}</select></td>
      <td class="c"><input type="checkbox" data-sf="auto" ${p.auto ? "checked" : ""} aria-label="Auto-update"></td>
      <td class="c">${isStack ? "" : `<input type="checkbox" data-sf="reboot" ${p.reboot ? "checked" : ""} aria-label="Reboot if needed">`}</td>
    </tr>`;
  };
  const body = S.targets.map((t) => `
    <tr class="grp"><td colspan="4">${esc(t.host)}</td></tr>
    ${row(`${t.host}/host`, t.kind === "pve" ? "<em>The Proxmox host itself</em>" : "<em>This server</em>")}
    ${(t.stacks || []).map((st) => row(`${t.host}/host:${st}`, `<span class="indent">🐳 ${esc(st)}</span>`, true)).join("")}
    ${t.containers.map((c) => row(`${t.host}/${c.vmid}`, `${esc(c.name)} <span class="vmid">${c.vmid}</span>${c.type === "vm" ? ' <span class="kind-tag">VM</span>' : ""}`)
      + (c.stacks || []).map((st) => row(`${t.host}/${c.vmid}:${st}`, `<span class="indent">🐳 ${esc(st)}</span>`, true)).join("")).join("")}`).join("");
  return `
    <p>Give a container, VM, host or Docker stack (🐳) a window. Its Update dialog then defaults to that window, and Update all queues it there.
      Tick <strong>Auto</strong> to have it checked and updated in its window without you clicking anything.</p>
    <div class="table-wrap"><table class="sched-table">
      <thead><tr><th>Target</th><th>Window</th><th class="c">Auto</th><th class="c">Reboot if needed</th></tr></thead>
      <tbody>${body}</tbody>
    </table></div>
    <div class="form-actions"><button type="button" class="btn small primary" data-act="sched-save">Save schedules</button></div>`;
}

let loginDraft = null;  // unsaved registry login edits (tokens only kept until saved)
const hubName = (r) => (r === "registry-1.docker.io" ? "docker.io" : r);

function dockerSection() {
  const d = S.docker;
  if (!loginDraft) loginDraft = d.logins.map((lg) => ({ ...lg, registry: hubName(lg.registry), token: "" }));
  const rows = loginDraft.map((lg, i) => `
    <div class="win-card login" data-login="${i}">
      <div class="fields">
        <label class="field"><span>Registry</span><input data-lf="registry" value="${esc(lg.registry)}" placeholder="docker.io" ${lg.from_env ? "disabled" : ""}></label>
        <label class="field"><span>Username</span><input data-lf="username" value="${esc(lg.username)}" autocomplete="off" ${lg.from_env ? "disabled" : ""}></label>
        <label class="field grow"><span>Password or access token</span><input data-lf="token" type="password" autocomplete="new-password" value="${esc(lg.token || "")}"
          placeholder="${lg.token_set ? "Saved. Leave blank to keep it" : "Personal access token"}" ${lg.from_env ? "disabled" : ""}></label>
      </div>
      ${lg.from_env ? `<p class="hint">Set by the DOCKERHUB_USERNAME / DOCKERHUB_TOKEN environment variables.</p>` : ""}
      <div class="form-actions">
        <button type="button" class="btn small" data-act="login-test" data-i="${i}">Test login</button>
        ${lg.from_env ? "" : `<button type="button" class="btn small ghost danger" data-act="login-del" data-i="${i}">Remove</button>`}
        <span class="login-result" data-result="${i}"></span>
      </div>
    </div>`).join("");
  return `
    <form data-form="docker">
      <label class="check"><input type="checkbox" name="enabled" ${d.enabled ? "checked" : ""}> <span>Check Docker Compose stacks for new image versions</span></label>
      <label class="check"><input type="checkbox" name="prune" ${d.prune ? "checked" : ""}> <span>Remove unused old images after updating a stack (<code>docker image prune</code>)</span></label>
      <h3>Registry logins</h3>
      <p class="hint">Without a login Docker Hub allows about 100 image lookups an hour per IP address; logging in raises that and lets Patchdeck check private images.
        For Docker Hub, use your username and a <strong>read-only personal access token</strong> (Docker Hub → Account settings → Personal access tokens), not your password.
        Other registries (ghcr.io, a private registry…) work the same way. Tokens are stored in config.yaml and never shown again.</p>
      <div class="windows">${rows || `<p class="muted">No logins. Public images are checked anonymously.</p>`}</div>
      <div class="form-actions">
        <button type="button" class="btn small" data-act="login-add">+ Add login</button>
        <button class="btn primary small">Save</button>
      </div>
    </form>`;
}

function collectLogins() {
  document.querySelectorAll("#s-docker [data-login]").forEach((card) => {
    const lg = loginDraft[Number(card.dataset.login)];
    card.querySelectorAll("[data-lf]").forEach((inp) => { if (!inp.disabled) lg[inp.dataset.lf] = inp.value.trim(); });
  });
}

function discordSection() {
  const d = S.discord;
  return `
    <form data-form="discord">
      <label class="field"><span>Webhook URL</span>
        <input name="webhook" type="password" autocomplete="off" placeholder="${d.webhook_set ? "Configured. Paste a new URL to replace it" : "https://discord.com/api/webhooks/…"}" ${d.from_env ? "disabled" : ""}>
      </label>
      ${d.from_env ? `<p class="hint">Set by the DISCORD_WEBHOOK environment variable.</p>` : `<p class="hint">Discord channel → Edit Channel → Integrations → Webhooks → New Webhook → Copy URL.</p>`}
      <label class="field"><span>After each scheduled check</span>
        <select name="on_check">
          <option value="always" ${d.on_check === "always" ? "selected" : ""}>Send a summary whenever updates are pending</option>
          <option value="new" ${d.on_check === "new" ? "selected" : ""}>Only when new updates appear</option>
          <option value="off" ${d.on_check === "off" ? "selected" : ""}>Don't send</option>
        </select>
      </label>
      <label class="check"><input type="checkbox" name="on_update" ${d.on_update ? "checked" : ""}> Send a message when updates you start finish</label>
      <p class="hint">A summary is always sent after each maintenance window that ran updates.</p>
      <div class="form-actions">
        <button class="btn primary small">Save</button>
        <button type="button" class="btn small" data-act="discord-test" ${d.webhook_set ? "" : "disabled"}>Send test message</button>
        ${d.webhook_set && !d.from_env ? `<button type="button" class="btn small ghost danger" data-act="discord-clear">Remove webhook</button>` : ""}
      </div>
    </form>`;
}

function snapshotSection() {
  const s = S.snapshots;
  return `
    <form data-form="snapshots">
      <label class="check"><input type="checkbox" name="enabled" ${s.enabled ? "checked" : ""}> Tick "Snapshot first" by default</label>
      <div class="fields">
        <label class="field narrow"><span>Snapshots to keep</span><input name="keep" type="number" min="1" max="50" value="${s.keep}"></label>
        <label class="field grow"><span>If a snapshot fails</span>
          <select name="on_failure">
            <option value="abort" ${s.on_failure === "abort" ? "selected" : ""}>Stop, don't update that container</option>
            <option value="continue" ${s.on_failure === "continue" ? "selected" : ""}>Update anyway without a snapshot</option>
          </select>
        </label>
      </div>
      <p class="hint">Only snapshots Patchdeck made (named <code>pre-update-…</code>) are ever removed.</p>
      <div class="form-actions"><button class="btn primary small">Save</button></div>
    </form>`;
}

function generalSection() {
  return `
    <form data-form="general">
      <label class="field"><span>Dashboard address (linked in Discord messages)</span>
        <input name="base_url" value="${esc(S.base_url)}" placeholder="http://patchdeck.your-tailnet.ts.net:8080">
      </label>
      <label class="check"><input type="checkbox" name="win_drivers" ${S.windows_updates.drivers ? "checked" : ""}> <span>Include driver updates for Windows VMs</span></label>
      <label class="field"><span>Hidden container IDs (comma separated)</span>
        <input name="exclude" value="${esc(S.exclude.join(", "))}" placeholder="e.g. 104, 120">
      </label>
      <div class="form-actions"><button class="btn primary small">Save</button></div>
    </form>`;
}

const SECTIONS = [
  ["hosts", "Proxmox hosts", hostsSection],
  ["key", "Patchdeck SSH key", keySection],
  ["schedule", "Check schedule", scheduleSection],
  ["windows", "Maintenance windows", windowsSection],
  ["schedules", "Maintenance schedules", schedulesSection],
  ["docker", "Docker", dockerSection],
  ["discord", "Discord notifications", discordSection],
  ["snapshots", "Snapshots", snapshotSection],
  ["general", "General", generalSection],
];

function renderSettings(only) {
  const root = $("#view-settings");
  if (!root.dataset.built) {
    root.innerHTML = `<div class="settings">${SECTIONS.map(([id, title]) =>
      `<section class="panel card" id="s-${id}"><h2>${title}</h2><div class="card-body"></div></section>`).join("")}</div>`;
    root.dataset.built = "1";
  }
  root.querySelector("#s-key").style.order = S.key.public ? "" : "-1";
  for (const [id, , fn] of SECTIONS) {
    if (!only || only.includes(id)) root.querySelector(`#s-${id} .card-body`).innerHTML = fn();
  }
}

async function loadSettings() {
  try {
    S = await api("/api/settings");
    winDraft = null;
    loginDraft = null;
    schedDraft = { check_times: [...S.schedule.check_times], days: [...S.schedule.days] };
    renderSettings();
  } catch (e) { toast(e.message); }
}

function applySettings(payload, only) {
  if (!payload) return;
  S = payload;
  if (!only || only.includes("schedule")) schedDraft = { check_times: [...S.schedule.check_times], days: [...S.schedule.days] };
  renderSettings(only);
}

/* ---------- events ---------- */

$("#view-settings").addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.copy !== undefined) return copyText(b.dataset.copy);
  if (b.dataset.wday !== undefined) {
    const on = b.classList.toggle("on");
    b.setAttribute("aria-pressed", on);
    return;
  }
  if (b.dataset.day !== undefined) {
    const d = Number(b.dataset.day);
    schedDraft.days = schedDraft.days.includes(d) ? schedDraft.days.filter((x) => x !== d) : [...schedDraft.days, d].sort();
    return renderSettings(["schedule"]);
  }
  if (b.dataset.delTime !== undefined) {
    schedDraft.check_times.splice(Number(b.dataset.delTime), 1);
    return renderSettings(["schedule"]);
  }
  const host = b.dataset.host;
  switch (b.dataset.act) {
    case "gen-key": applySettings(await act(b, "/api/key/generate", {}, "Generating…"), ["key", "hosts"]); break;
    case "fp": {
      const r = await act(b, "/api/hosts/fingerprint", { name: host }, "Connecting…");
      if (r) { fp[host] = r; renderSettings(["hosts"]); }
      break;
    }
    case "fp-cancel": delete fp[host]; renderSettings(["hosts"]); break;
    case "trust": {
      const r = await act(b, "/api/hosts/trust", { name: host, fingerprint: fp[host].fingerprint }, "Saving…");
      if (r) { delete fp[host]; toast(`${host} fingerprint trusted`); applySettings(r, ["hosts"]); }
      break;
    }
    case "toggle-manual": {
      const m = $(`[data-manual="${CSS.escape(host)}"]`);
      if (m) m.hidden = !m.hidden;
      break;
    }
    case "test": {
      busy(b, true, "Testing…");
      try { tested[host] = { ok: true, ...(await api("/api/hosts/test", { name: host })) }; }
      catch (err) { tested[host] = { ok: false, error: err.message }; }
      renderSettings(["hosts"]);
      if (tested[host].ok) refresh();
      break;
    }
    case "expand": expanded.has(host) ? expanded.delete(host) : expanded.add(host); renderSettings(["hosts"]); break;
    case "edit": editing.add(host); renderSettings(["hosts"]); break;
    case "edit-cancel": editing.delete(host); renderSettings(["hosts"]); break;
    case "remove":
      if (!confirm(`Remove ${host} from Patchdeck? Its containers will disappear from the dashboard. Nothing changes on the host.`)) return;
      applySettings(await act(b, "/api/hosts/delete", { name: host }, "Removing…"), ["hosts", "schedule", "schedules"]);
      refresh();
      break;
    case "login-add":
      collectLogins();
      loginDraft.push({ registry: loginDraft.some((l) => hubName(l.registry) === "docker.io") ? "" : "docker.io", username: "", token: "" });
      renderSettings(["docker"]);
      break;
    case "login-del":
      collectLogins();
      loginDraft.splice(Number(b.dataset.i), 1);
      renderSettings(["docker"]);
      break;
    case "login-test": {
      collectLogins();
      const lg = loginDraft[Number(b.dataset.i)];
      const out = document.querySelector(`#s-docker [data-result="${b.dataset.i}"]`);
      out.textContent = "";
      const r = await act(b, "/api/settings/registry-test", { registry: lg.registry, username: lg.username, token: lg.token }, "Testing…");
      if (r) { out.textContent = (r.ok ? "✓ " : "✗ ") + r.message; out.className = `login-result ${r.ok ? "ok-text" : "bad-text"}`; }
      break;
    }
    case "win-add":
      collectWindows();
      winDraft.push({ name: "", days: [5, 6], start: "02:00", end: "05:00" });
      renderSettings(["windows"]);
      break;
    case "win-del": {
      collectWindows();
      const w = winDraft[Number(b.dataset.i)];
      const used = w.was && Object.values(S.schedules).some((p) => p.window === w.was);
      if (used && !confirm(`"${w.was}" is used by some schedules. Removing it clears those schedules. Continue?`)) return;
      winDraft.splice(Number(b.dataset.i), 1);
      renderSettings(["windows"]);
      break;
    }
    case "win-save": {
      collectWindows();
      if (winDraft.some((w) => !w.name)) return toast("Give every window a name");
      const r = await act(b, "/api/settings", { windows: winDraft }, "Saving…");
      if (r) { winDraft = null; toast("Maintenance windows saved"); applySettings(r, ["windows", "schedules"]); refresh(); }
      break;
    }
    case "sched-save": {
      const schedules = {};
      document.querySelectorAll("#s-schedules tr[data-key]").forEach((tr) => {
        const win = tr.querySelector('[data-sf="window"]').value;
        const rb = tr.querySelector('[data-sf="reboot"]');
        if (win) schedules[tr.dataset.key] = { window: win, auto: tr.querySelector('[data-sf="auto"]').checked, reboot: !!(rb && rb.checked) };
      });
      const r = await act(b, "/api/settings", { schedules }, "Saving…");
      if (r) { toast("Schedules saved"); applySettings(r, ["schedules"]); refresh(); }
      break;
    }
    case "add-time":
      schedDraft.check_times.push("12:00");
      renderSettings(["schedule"]);
      break;
    case "save-schedule": {
      if (!schedDraft.days.length || !schedDraft.check_times.length) {
        if (!confirm("No days or no times selected: scheduled checks and Discord summaries will stop. Save anyway?")) return;
      }
      const r = await act(b, "/api/settings", { schedule: schedDraft }, "Saving…");
      if (r) { toast("Schedule saved"); applySettings(r, ["schedule"]); refresh(); }
      break;
    }
    case "discord-test":
      if (await act(b, "/api/settings/discord-test", {}, "Sending…")) toast("Test message sent, check Discord");
      break;
    case "discord-clear":
      if (!confirm("Remove the Discord webhook?")) return;
      applySettings(await act(b, "/api/settings", { discord: { clear_webhook: true } }, "Removing…"), ["discord"]);
      break;
  }
});

$("#view-settings").addEventListener("change", (e) => {
  if (e.target.dataset.time !== undefined) schedDraft.check_times[Number(e.target.dataset.time)] = e.target.value;
});

$("#view-settings").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const btn = f.querySelector("button:not([type=button])");
  const v = (n) => f.elements[n] && f.elements[n].value;
  switch (f.dataset.form) {
    case "host": {
      const original = f.dataset.original;
      const r = await act(btn, "/api/hosts", { name: v("name").trim(), address: v("address").trim(), port: Number(v("port") || 22), type: v("type"), original }, "Saving…");
      if (r) {
        if (original) { editing.delete(original); delete tested[original]; delete fp[original]; }
        toast(original ? "Host saved" : `${v("name")} added. Now trust its fingerprint`);
        applySettings(r, ["hosts", "schedule", "schedules"]);
        refresh();
      }
      break;
    }
    case "install": {
      const host = f.dataset.host;
      const r = await act(btn, "/api/hosts/install-key", { name: host, password: v("password") }, "Installing…");
      f.elements.password.value = "";
      if (r) {
        toast("Key installed, testing…");
        try { tested[host] = { ok: true, ...(await api("/api/hosts/test", { name: host })) }; }
        catch (err) { tested[host] = { ok: false, error: err.message }; }
        renderSettings(["hosts"]);
        if (tested[host].ok) refresh();
      }
      break;
    }
    case "docker": {
      collectLogins();
      const logins = loginDraft.filter((l) => !l.from_env && (l.username || l.token))
        .map((l) => ({ registry: l.registry || "docker.io", username: l.username, token: l.token }));
      const r = await act(btn, "/api/settings", { docker: { enabled: f.elements.enabled.checked, prune: f.elements.prune.checked, logins } }, "Saving…");
      if (r) { loginDraft = null; toast("Docker settings saved"); applySettings(r, ["docker"]); refresh(); }
      break;
    }
    case "discord": {
      const body = { on_check: v("on_check"), on_update: f.elements.on_update.checked };
      if (v("webhook")) body.webhook = v("webhook").trim();
      const r = await act(btn, "/api/settings", { discord: body }, "Saving…");
      if (r) { toast("Discord settings saved"); applySettings(r, ["discord"]); }
      break;
    }
    case "snapshots": {
      const r = await act(btn, "/api/settings", { snapshots: { enabled: f.elements.enabled.checked, keep: Number(v("keep")), on_failure: v("on_failure") } }, "Saving…");
      if (r) { toast("Snapshot settings saved"); applySettings(r, ["snapshots"]); }
      break;
    }
    case "general": {
      const exclude = v("exclude").split(/[\s,]+/).filter(Boolean);
      if (exclude.some((x) => !/^\d+$/.test(x))) return toast("Container IDs must be numbers");
      const r = await act(btn, "/api/settings", { base_url: v("base_url"), exclude: exclude.map(Number),
        windows_updates: { drivers: f.elements.win_drivers.checked } }, "Saving…");
      if (r) { toast("Saved"); applySettings(r, ["general"]); refresh(); }
      break;
    }
  }
});

/* ---------- view switching ---------- */

function showView() {
  const settings = location.hash === "#settings";
  $("#view-dash").hidden = settings;
  $("#view-settings").hidden = !settings;
  $("#dash-actions").hidden = settings;
  $("#log").hidden = settings || $("#log").hidden;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", (t.dataset.view === "settings") === settings));
  if (settings) loadSettings();
}
window.addEventListener("hashchange", showView);
showView();
