"""Patchdeck: a small update dashboard for Proxmox hosts and their LXC containers."""
import asyncio
import base64
import contextlib
import copy
import datetime as dt
import json
import logging
import os
import re
import shutil
import secrets
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import sshsetup
from .notify import discord
from . import registry
from .proxmox import APK_UPGRADE, APT_UPGRADE, HOST_KINDS, Host, family, stack_update_script

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("patchdeck")
logging.getLogger("asyncssh").setLevel(logging.WARNING)

CONFIG_PATH = Path(os.environ.get("PATCHDECK_CONFIG", "/data/config.yaml"))
DATA_DIR = Path(os.environ.get("PATCHDECK_DATA", CONFIG_PATH.parent))
STATIC = Path(__file__).parent / "static"


# ---------------------------------------------------------------- config
# config.yaml is the source of truth. The Settings page edits it (RAW) and the app reloads
# the derived values (CFG and the globals below) without a restart. Env vars override secrets.

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def read_raw():
    return yaml.safe_load(CONFIG_PATH.read_text()) or {}


def hhmm(t):
    # YAML reads an unquoted 18:30 as the number 1110 (minutes); turn it back into "18:30".
    return f"{t // 60:02d}:{t % 60:02d}" if isinstance(t, int) else str(t)


def normalise_maintenance(cfg):
    """Maintenance windows + per-target schedules. Migrates the older single-window format
    (maintenance.enabled/days/start/end + maintenance_only)."""
    mw = cfg.get("maintenance") if isinstance(cfg.get("maintenance"), dict) else {}
    windows = mw.get("windows")
    if windows is None:
        windows = []
        if mw.get("enabled"):
            windows = [{"name": "Default", "days": mw.get("days", list(range(7))),
                        "start": mw.get("start", "02:00"), "end": mw.get("end", "05:00")}]
    out, seen = [], set()
    for w in windows:
        name = str(w.get("name") or "").strip()
        if not name or name in seen:
            raise ValueError("Each maintenance window needs a unique name")
        seen.add(name)
        start, end = hhmm(w.get("start", "02:00")), hhmm(w.get("end", "05:00"))
        for t in (start, end):
            if not TIME_RE.match(t):
                raise ValueError(f"Invalid time {t!r} in window {name!r}, use HH:MM")
        if start == end:
            raise ValueError(f"Window {name!r}: start and end can't be the same")
        days = sorted({int(d) for d in w.get("days", range(7))})
        if any(d < 0 or d > 6 for d in days):
            raise ValueError(f"Window {name!r}: invalid day")
        out.append({"name": name, "days": days, "start": start, "end": end})
    schedules = {}
    for key, pol in (cfg.get("schedules") or {}).items():
        pol = pol or {}
        win = pol.get("window")
        if win not in seen:
            continue  # its window was removed
        schedules[str(key)] = {"window": win, "auto": bool(pol.get("auto")), "reboot": bool(pol.get("reboot"))}
    for key in cfg.get("maintenance_only") or []:  # old format
        if out:
            schedules.setdefault(str(key), {"window": out[0]["name"], "auto": False, "reboot": False})
    return {"windows": out}, schedules


def effective(raw):
    cfg = copy.deepcopy(raw)
    for key in ("auth", "ssh", "schedule", "discord", "snapshots"):
        if not isinstance(cfg.get(key), dict):
            cfg[key] = {}
    cfg["schedule"].setdefault("check_times", ["08:00"])
    cfg["schedule"].setdefault("days", list(range(7)))
    cfg["exclude"] = cfg.get("exclude") or []
    dk = cfg.get("docker") if isinstance(cfg.get("docker"), dict) else {}
    logins = []
    for lg in dk.get("logins") or []:
        if isinstance(lg, dict) and lg.get("username"):
            logins.append({"registry": registry.normalise_registry(lg.get("registry")),
                           "username": str(lg["username"]), "token": str(lg.get("token") or "")})
    if os.environ.get("DOCKERHUB_USERNAME") and os.environ.get("DOCKERHUB_TOKEN"):
        logins = [lg for lg in logins if lg["registry"] != "registry-1.docker.io"]
        logins.append({"registry": "registry-1.docker.io", "username": os.environ["DOCKERHUB_USERNAME"],
                       "token": os.environ["DOCKERHUB_TOKEN"], "env": True})
    wn = cfg.get("windows_updates") if isinstance(cfg.get("windows_updates"), dict) else {}
    cfg["windows_updates"] = {"drivers": bool(wn.get("drivers", False))}
    cfg["docker"] = {"enabled": bool(dk.get("enabled", True)), "prune": bool(dk.get("prune", True)),
                     "logins": logins}
    cfg["maintenance"], cfg["schedules"] = normalise_maintenance(cfg)
    cfg["hosts"] = cfg.get("hosts") or []
    if os.environ.get("PATCHDECK_PASSWORD"):
        cfg["auth"]["password"] = os.environ["PATCHDECK_PASSWORD"]
    if os.environ.get("DISCORD_WEBHOOK"):
        cfg["discord"]["webhook"] = os.environ["DISCORD_WEBHOOK"]
    if not cfg["auth"].get("password") and not cfg["auth"].get("disabled"):
        raise SystemExit("Set auth.password in config.yaml (or PATCHDECK_PASSWORD).")
    cfg["schedule"]["check_times"] = [hhmm(t) for t in cfg["schedule"]["check_times"] or []]
    for t in cfg["schedule"]["check_times"]:
        if not TIME_RE.match(str(t)):
            raise ValueError(f"Invalid check time {t!r}, use HH:MM")
    return cfg


RAW = read_raw()
CFG = effective(RAW)
HOSTS = {}
HOST_LOCKS = {}
SCHEDULE_CHANGED = asyncio.Event()
MAINT_CHANGED = asyncio.Event()


def apply_config():
    """Rebuild everything derived from CFG."""
    global SSH, KNOWN_HOSTS, KEY_FILE, HOSTS, SNAP, EXCLUDE, DISCORD, WINDOWS, SCHEDULES, DOCKER
    SSH = CFG["ssh"]
    kh = SSH.get("known_hosts", str(DATA_DIR / "known_hosts"))
    KNOWN_HOSTS = None if kh in ("none", "off", False) else kh
    KEY_FILE = SSH.get("key_file", str(DATA_DIR / "id_ed25519"))
    HOSTS = {
        h["name"]: Host(
            name=h["name"], address=h["address"], port=int(h.get("port", 22)),
            user=h.get("user", SSH.get("user", "root")),
            key_file=h.get("key_file", KEY_FILE),
            known_hosts=KNOWN_HOSTS,
            console_url=h.get("console_url", CFG.get("console_url")),
            host_console_url=h.get("host_console_url", CFG.get("host_console_url")),
            kind=h.get("type", "pve"),
        )
        for h in CFG["hosts"]
    }
    for name, h in HOSTS.items():
        HOST_LOCKS.setdefault(name, asyncio.Lock())
        h.win_drivers = CFG["windows_updates"]["drivers"]
    SNAP = {"enabled": bool(CFG["snapshots"].get("enabled", True)),
            "keep": int(CFG["snapshots"].get("keep", 3)),
            "on_failure": CFG["snapshots"].get("on_failure", "abort")}
    EXCLUDE = {int(v) for v in CFG["exclude"]}
    DISCORD = CFG["discord"]
    DOCKER = CFG["docker"]
    WINDOWS = CFG["maintenance"]["windows"]
    SCHEDULES = CFG["schedules"]


apply_config()
if KNOWN_HOSTS is None:
    log.warning("SSH host key checking is DISABLED (ssh.known_hosts: none)")


def save_config():
    """Validate RAW, write it to config.yaml (keeping a one-time backup), and reload."""
    global CFG
    cfg = effective(RAW)
    backup = CONFIG_PATH.with_name(CONFIG_PATH.name + ".bak")
    if CONFIG_PATH.exists() and not backup.exists():
        shutil.copy2(CONFIG_PATH, backup)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text("# Managed by Patchdeck's Settings page; comments are not preserved.\n"
                   + yaml.safe_dump(RAW, sort_keys=False, allow_unicode=True))
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)
    CFG = cfg
    apply_config()
    SCHEDULE_CHANGED.set()
    MAINT_CHANGED.set()


# ---------------------------------------------------------------- state

STATE_FILE = DATA_DIR / "state.json"
JOBS_FILE = DATA_DIR / "jobs.json"


def _load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


STATE = _load(STATE_FILE, {"hosts": {}, "notified": []})
STATE.setdefault("hosts", {})
STATE.setdefault("notified", [])
STATE.setdefault("scheduled", [])  # updates waiting for the maintenance window
STATE["next_check"] = None


def save_state():
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(STATE))
    tmp.replace(STATE_FILE)


class Job:
    def __init__(self, kind, host, vmid=None, name=None, snapshot=False, batch=None, auto=False):
        self.id = secrets.token_hex(6)
        self.kind, self.host, self.vmid, self.name = kind, host, vmid, name
        self.snapshot, self.batch, self.auto = snapshot, batch, auto
        self.quiet = False  # maintenance-window jobs report in one summary instead
        self.reboot = False  # reboot afterwards if the update leaves a reboot pending
        self.stack = None    # set for a Docker Compose stack update
        self.status = "queued"
        self.summary = ""
        self.lines = []
        self.created, self.started, self.finished = time.time(), None, None
        self.subs = set()

    def log(self, line):
        self.lines.append(line)
        if len(self.lines) > 5000:
            del self.lines[:1000]
        for q in self.subs:
            q.put_nowait(("line", line))

    def start(self):
        self.status, self.started = "running", time.time()
        for q in self.subs:
            q.put_nowait(("status", self.status))

    def finish(self, status, summary=""):
        self.status, self.summary, self.finished = status, summary, time.time()
        self.log(f"── {status.upper()}{': ' + summary if summary else ''}")
        for q in self.subs:
            q.put_nowait(("end", status))
        save_jobs()

    def public(self):
        return {k: getattr(self, k) for k in
                ("id", "kind", "host", "vmid", "name", "status", "summary", "created", "started", "finished",
                 "batch", "stack")}

    @classmethod
    def restore(cls, d):
        j = cls(d["kind"], d["host"], d.get("vmid"), d.get("name"))
        j.stack = None
        for k, v in d.items():
            setattr(j, k, v)
        if j.status in ("queued", "running"):
            j.status, j.summary = "failed", "Interrupted by restart"
        j.subs = set()
        return j


JOBS = {}
for d in _load(JOBS_FILE, []):
    j = Job.restore(d)
    JOBS[j.id] = j
TASKS = set()


def save_jobs():
    recent = sorted(JOBS.values(), key=lambda j: j.created)[-60:]
    for j in list(JOBS):
        if JOBS[j] not in recent:
            del JOBS[j]
    data = [dict(j.public(), lines=j.lines[-400:]) for j in recent]
    JOBS_FILE.write_text(json.dumps(data))


def spawn(coro):
    t = asyncio.create_task(coro)
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return t


def active_job(host, vmid, stack=None):
    return next((j for j in JOBS.values() if j.host == host and j.vmid == vmid and j.stack == stack
                 and j.kind == "update" and j.status in ("queued", "running")), None)


def find_ct(host, vmid):
    h = STATE["hosts"].get(host) or {}
    return next((c for c in h.get("containers", []) if c["vmid"] == vmid), None)


def find_target(host, vmid):
    """The state entry for a guest, or the host's own entry when vmid is None."""
    return STATE["hosts"].get(host, {}).get("host") if vmid is None else find_ct(host, vmid)


def find_stack(host, vmid, stack):
    t = find_target(host, vmid) or {}
    return next((st for st in t.get("stacks") or [] if st["name"] == stack), None)


def target_kind(host, vmid):
    return "host" if vmid is None else (find_ct(host, vmid) or {}).get("type", "lxc")


def target_name(host, vmid, stack=None):
    base = f"{host} (host)" if vmid is None else (find_ct(host, vmid) or {}).get("name", str(vmid))
    return f"{base} / {stack}" if stack else base


async def annotate_docker(entries):
    """Ask registries whether each stack's images have newer versions."""
    targets = [e for e in entries if e and e.get("stacks")]
    if not targets or not DOCKER["enabled"]:
        return
    reg = registry.Registry(logins={lg["registry"]: (lg["username"], lg["token"])
                                    for lg in DOCKER["logins"] if lg["token"]})
    try:
        await asyncio.gather(*(registry.annotate(e["stacks"], reg) for e in targets))
    finally:
        await reg.close()


# ---------------------------------------------------------------- checks

async def run_check(job):
    h = HOSTS[job.host]
    async with HOST_LOCKS[job.host]:
        job.start()
        job.log(f"Connecting to {h.address}…")
        try:
            result = await h.check(exclude=EXCLUDE)
            if DOCKER["enabled"]:
                job.log("Checking Docker images against their registries…")
                await annotate_docker([result["host"]] + result["containers"])
            STATE["hosts"][job.host] = result
            save_state()
            n_ct = sum(len(c["packages"]) for c in result["containers"])
            for c in result["containers"]:
                detail = c["error"] or (c["status"] if c["status"] != "running" else f"{len(c['packages'])} updates")
                job.log(f"  {c['vmid']:>5}  {c['name']:<24} {detail}")
                for st in c.get("stacks") or []:
                    job.log(f"         🐳 {st['name']:<21} {st.get('updates', 0)} image update(s)")
            job.finish("success", f"host {len(result['host']['packages'])} · containers {n_ct} updates")
        except Exception as e:
            prev = STATE["hosts"].get(job.host, {})
            prev["error"] = str(e)
            STATE["hosts"][job.host] = prev
            save_state()
            job.log(f"ERROR: {e}")
            job.finish("failed", str(e))


async def check_all(auto=False):
    jobs = [Job("check", name, auto=auto) for name in HOSTS]
    for j in jobs:
        JOBS[j.id] = j
    await asyncio.gather(*(run_check(j) for j in jobs))
    if auto:
        await notify_check()
    return jobs


def pending_targets():
    out = []
    for hname in HOSTS:
        h = STATE["hosts"].get(hname)
        if not h:
            continue
        if h.get("host", {}).get("packages"):
            out.append((hname, None, f"{hname} (host)", h["host"]["packages"]))
        for c in h.get("containers", []):
            if c["packages"]:
                out.append((hname, c["vmid"], c["name"], c["packages"]))
    return out


def pending_stacks():
    """(host, vmid, stack name, display name, number of images with updates)."""
    out = []
    for hname in HOSTS:
        h = STATE["hosts"].get(hname) or {}
        for vmid, t in [(None, h.get("host") or {})] + [(c["vmid"], c) for c in h.get("containers", [])]:
            if vmid in EXCLUDE or (vmid is not None and t.get("status") != "running"):
                continue
            for st in t.get("stacks") or []:
                if st.get("updates"):
                    out.append((hname, vmid, st["name"], target_name(hname, vmid, st["name"]), st["updates"]))
    return out


async def notify_check():
    mode = DISCORD.get("on_check", "always")
    if mode == "off" or not DISCORD.get("webhook"):
        return
    targets = pending_targets()
    stacks = pending_stacks() if DOCKER["enabled"] else []
    sig = sorted(f"{h}:{v}:{p['name']}:{p['to']}" for h, v, _, pk in targets for p in pk)
    sig += sorted(f"{h}:{v}:{st}:{i['ref']}:{i.get('remote')}" for h, v, st, _, _ in stacks
                  for i in (find_stack(h, v, st) or {}).get("images", []) if i.get("status") == "update")
    if not (targets or stacks) or (mode == "new" and set(sig) <= set(STATE["notified"])):
        return
    total = sum(len(pk) for *_, pk in targets)
    lines = [f"🔔 **{total} updates available** across {len(targets)} target(s)" if targets
             else "🔔 **Docker image updates available**"]
    for h, vmid, name, pk in targets:
        where = f"`{h}`" if vmid is None else f"`{h}/{vmid}`"
        lines.append(f"• **{name}** {where}: {len(pk)}")
    for h, vmid, st, name, n in stacks:
        lines.append(f"🐳 **{name}** `{h}`: {n} image{'s' if n != 1 else ''}")
    errors = [f"{n}/{c['name']}" for n, h in STATE["hosts"].items()
              for c in h.get("containers", []) if c.get("error")]
    errors += [n for n, h in STATE["hosts"].items() if h.get("error")]
    if errors:
        lines.append(f"⚠️ Could not check: {', '.join(errors)}")
    if CFG.get("base_url"):
        lines.append(f"<{CFG['base_url']}>")
    await discord(DISCORD["webhook"], "\n".join(lines))
    STATE["notified"] = sig
    save_state()


# ---------------------------------------------------------------- updates

async def take_snapshot(job, h, kind):
    job.log("Taking snapshot…")
    try:
        snap = await h.snapshot(job.vmid, kind)
        job.log(f"Snapshot created: {snap}")
    except Exception as e:
        job.log(f"Snapshot failed: {e}")
        if SNAP["on_failure"] == "abort":
            raise RuntimeError("Snapshot failed, update aborted (change this under Settings → Snapshots)")
        job.log("Continuing without a snapshot")
        job.snapshot = False


async def prune_snapshots(job, h, kind):
    removed = await h.prune_snapshots(job.vmid, SNAP["keep"], kind)
    if removed:
        job.log(f"Removed old snapshots: {', '.join(removed)}")


async def refresh_docker(host, vmid):
    h = HOSTS[host]
    d = await h.check_docker(target_kind(host, vmid), vmid)
    await annotate_docker([d])
    target = find_target(host, vmid)
    if target is not None:
        target["stacks"], target["docker_error"] = d.get("stacks"), d.get("docker_error")
        save_state()


async def run_stack_update(job):
    h = HOSTS[job.host]
    async with HOST_LOCKS[job.host]:
        job.start()
        try:
            st = find_stack(job.host, job.vmid, job.stack)
            if not st:
                raise RuntimeError("Stack not in the last check; run a check first")
            kind = target_kind(job.host, job.vmid)
            before = st.get("updates", 0)
            if job.snapshot and kind != "host":
                await take_snapshot(job, h, kind)
            job.log(f"$ docker compose pull && docker compose up -d   ({st['name']})")
            rc = await h.run_in(kind, job.vmid, stack_update_script(st["name"], st["files"], DOCKER["prune"]), job.log)
            if rc == 0 and job.snapshot and kind != "host":
                await prune_snapshots(job, h, kind)
            job.log("Re-checking images…")
            await refresh_docker(job.host, job.vmid)
            after = find_stack(job.host, job.vmid, job.stack) or {}
            if rc != 0:
                job.finish("failed", f"exit code {rc}")
            else:
                job.finish("success", f"{before} → {after.get('updates', 0)} image update(s)")
        except Exception as e:
            job.log(f"ERROR: {e}")
            job.finish("failed", str(e))
    await notify_update(job)


async def run_update(job):
    if job.stack:
        return await run_stack_update(job)
    h = HOSTS[job.host]
    async with HOST_LOCKS[job.host]:
        job.start()
        try:
            if job.vmid is None:
                before = len(STATE["hosts"].get(job.host, {}).get("host", {}).get("packages", []))
                job.log(f"$ apt-get dist-upgrade on {job.host}")
                rc = await h.stream(h.upgrade_cmd(), job.log)
                kind = "host"
            else:
                ct = find_ct(job.host, job.vmid)
                if not ct:
                    raise RuntimeError("Not in the last check; run a check first")
                kind = ct.get("type", "lxc")
                fam = family(ct["os"])
                if not fam:
                    raise RuntimeError(f"Unsupported OS: {ct['os']}")
                before = len(ct["packages"])
                if job.snapshot:
                    await take_snapshot(job, h, kind)
                job.log(f"$ {({'apt': 'apt-get dist-upgrade', 'apk': 'apk upgrade', 'win': 'Windows Update'})[fam]} in {ct['name']}")
                if fam == "win":
                    job.log("(Windows output arrives through the guest agent every few seconds; searching alone can take minutes)")
                    rc = await h.run_windows(job.vmid, job.log)
                elif kind == "vm":
                    job.log("(VM output arrives through the guest agent every few seconds)")
                    rc = await h.run_vm(job.vmid, APT_UPGRADE if fam == "apt" else APK_UPGRADE, job.log)
                else:
                    rc = await h.stream(h.upgrade_cmd(job.vmid, fam), job.log)
                if rc == 0 and job.snapshot:
                    await prune_snapshots(job, h, kind)
            job.log("Re-checking…")
            after = await recheck(job)
            notes = []
            if rc == 0 and after.get("reboot") and job.reboot:
                if kind == "host":
                    job.log("Reboot required: rebooting the host in 1 minute")
                    await h.reboot_self()
                    notes.append("rebooting")
                    spawn(delayed_check(job.host, 420))
                else:
                    job.log(f"Reboot required: rebooting {job.name}…")
                    is_win = (find_ct(job.host, job.vmid) or {}).get("os") == "mswindows"
                    await h.reboot_guest(job.vmid, kind, job.log, wait=1200 if is_win else 300)
                    after = await recheck(job)
                    notes.append("rebooted")
            elif after.get("reboot"):
                notes.append("reboot required")
            if rc != 0:
                job.finish("failed", f"exit code {rc}")
            else:
                job.finish("success", " · ".join([f"{before} → {len(after['packages'])} pending"] + notes))
        except Exception as e:
            job.log(f"ERROR: {e}")
            job.finish("failed", str(e))
    await notify_update(job)


async def recheck(job):
    h = HOSTS[job.host]
    if job.vmid is None:
        STATE["hosts"][job.host]["host"] = after = await h.check_self()
    else:
        after = await h.check_guest(job.vmid)
        cts = STATE["hosts"][job.host]["containers"]
        cts[:] = [after if c["vmid"] == job.vmid else c for c in cts]
    save_state()
    return after


async def delayed_check(host, delay):
    """Re-check a host after it has had time to reboot."""
    await asyncio.sleep(delay)
    if host in HOSTS:
        j = Job("check", host)
        JOBS[j.id] = j
        await run_check(j)


async def notify_update(job):
    if job.quiet or not DISCORD.get("on_update", True) or not DISCORD.get("webhook"):
        return
    group = [job]
    if job.batch:
        group = [j for j in JOBS.values() if j.batch == job.batch]
        if any(j.status in ("queued", "running") for j in group):
            return  # the last job in the batch sends the summary
    icon = {"success": "✅", "failed": "❌"}
    lines = ["🛠️ **Update run finished**" if len(group) > 1 else ""]
    for j in group:
        lines.append(f"{icon.get(j.status, '•')} **{j.name}** `{j.host}`: {j.summary}")
    await discord(DISCORD["webhook"], "\n".join(l for l in lines if l))


def queue_update(host, vmid, snapshot, batch=None, reboot=False, stack=None):
    if host not in HOSTS:
        raise HTTPException(404, "Unknown host")
    if active_job(host, vmid, stack):
        raise HTTPException(409, "An update for this target is already queued or running")
    job = Job("update", host, vmid, target_name(host, vmid, stack), snapshot=snapshot and vmid is not None, batch=batch)
    job.reboot = bool(reboot) and not stack
    job.stack = stack
    JOBS[job.id] = job
    if is_scheduled(host, vmid, stack):  # updating now replaces a pending maintenance-window update
        unschedule(host, vmid, stack)
    spawn(run_update(job))
    return job


# ---------------------------------------------------------------- scheduler

def next_run(now=None):
    sched = CFG["schedule"]
    times = sorted({tuple(map(int, t.split(":"))) for t in sched.get("check_times") or []})
    days = {int(d) for d in sched.get("days", range(7))}
    if not times or not days or not HOSTS:
        return None
    now = now or dt.datetime.now()
    for offset in range(8):
        day = (now + dt.timedelta(days=offset)).date()
        if day.weekday() not in days:
            continue
        for hh, mm in times:
            c = dt.datetime.combine(day, dt.time(hh, mm))
            if c > now:
                return c
    return None


async def scheduler():
    if not STATE["hosts"] and HOSTS:
        await check_all(auto=False)
    while True:
        SCHEDULE_CHANGED.clear()
        nxt = next_run()
        STATE["next_check"] = nxt.timestamp() if nxt else None
        wait = max(1, (nxt - dt.datetime.now()).total_seconds()) if nxt else None
        try:
            await asyncio.wait_for(SCHEDULE_CHANGED.wait(), timeout=wait)
            continue  # settings changed: work out the next run again
        except asyncio.TimeoutError:
            pass
        try:
            await check_all(auto=True)
        except Exception:
            log.exception("Scheduled check failed")


# ---------------------------------------------------------------- maintenance windows

def target_key(host, vmid, stack=None):
    return f"{host}/{'host' if vmid is None else vmid}" + (f":{stack}" if stack else "")


def _same(i, host, vmid, stack):
    return i["host"] == host and i["vmid"] == vmid and i.get("stack") == stack


def is_scheduled(host, vmid, stack=None):
    return any(_same(i, host, vmid, stack) for i in STATE["scheduled"])


def scheduled_item(host, vmid, stack=None):
    return next((i for i in STATE["scheduled"] if _same(i, host, vmid, stack)), None)


def unschedule(host, vmid, stack=None):
    STATE["scheduled"] = [i for i in STATE["scheduled"] if not _same(i, host, vmid, stack)]
    save_state()
    MAINT_CHANGED.set()


def window_named(name):
    return next((w for w in WINDOWS if w["name"] == name), None)


def item_window(item):
    w = window_named(item.get("window")) or (WINDOWS[0] if WINDOWS else None)
    return w["name"] if w else None


def occurrence(w, now=None):
    """The run of window `w` in progress now, or its next one, as (start, end)."""
    if not w.get("days"):
        return None
    now = now or dt.datetime.now()
    sh, sm = map(int, w["start"].split(":"))
    eh, em = map(int, w["end"].split(":"))
    for offset in range(-1, 8):  # -1: a window that started yesterday may still be open
        day = (now + dt.timedelta(days=offset)).date()
        if day.weekday() not in w["days"]:
            continue
        start = dt.datetime.combine(day, dt.time(sh, sm))
        end = dt.datetime.combine(day, dt.time(eh, em))
        if end <= start:
            end += dt.timedelta(days=1)
        if end > now:
            return start, end
    return None


def window_info(name):
    w = window_named(name)
    occ = occurrence(w) if w else None
    return {"name": name, "start": occ[0].timestamp() if occ else None,
            "end": occ[1].timestamp() if occ else None} if w else None


def schedule_target(host, vmid, snapshot, reboot, window=None, stack=None):
    if host not in HOSTS:
        raise HTTPException(404, "Unknown host")
    if not WINDOWS:
        raise HTTPException(400, "Add a maintenance window in Settings first")
    window = window or SCHEDULES.get(target_key(host, vmid, stack), {}).get("window") or WINDOWS[0]["name"]
    if not window_named(window):
        raise HTTPException(400, "Unknown maintenance window")
    if active_job(host, vmid, stack):
        raise HTTPException(409, "An update for this target is already running")
    item = scheduled_item(host, vmid, stack)
    if item is None:
        item = {"host": host, "vmid": vmid, "stack": stack, "name": target_name(host, vmid, stack),
                "added": time.time()}
        STATE["scheduled"].append(item)
    item.update(window=window, snapshot=bool(snapshot) and vmid is not None, reboot=bool(reboot) and not stack)
    save_state()
    MAINT_CHANGED.set()


def split_key(key):
    """'pve1/105:immich' -> ('pve1', 105, 'immich'); 'pbs/host' -> ('pbs', None, None)."""
    host, _, rest = key.rpartition("/")
    target, _, stack = rest.partition(":")
    return host, (None if target == "host" else int(target)), (stack or None)


async def queue_auto_updates(w):
    """At the start of a window: re-check the hosts involved, then queue every target set to
    auto-update in this window that has updates waiting."""
    keys = [k for k, p in SCHEDULES.items() if p["auto"] and p["window"] == w["name"]]
    hosts = sorted({split_key(k)[0] for k in keys} & set(HOSTS))
    if not hosts:
        return
    jobs = [Job("check", h, auto=True) for h in hosts]
    for j in jobs:
        JOBS[j.id] = j
    await asyncio.gather(*(run_check(j) for j in jobs))
    for k in keys:
        host, vmid, stack = split_key(k)
        if host not in HOSTS or is_scheduled(host, vmid, stack) or active_job(host, vmid, stack):
            continue
        target = find_target(host, vmid)
        if not target or (vmid is not None and target.get("status") != "running"):
            continue
        pending = (find_stack(host, vmid, stack) or {}).get("updates") if stack else len(target.get("packages") or [])
        if pending:
            STATE["scheduled"].append({"host": host, "vmid": vmid, "stack": stack, "name": target_name(host, vmid, stack),
                                       "added": time.time(), "window": w["name"],
                                       "snapshot": SNAP["enabled"] and vmid is not None,
                                       "reboot": SCHEDULES[k]["reboot"] and not stack, "auto": True})
    save_state()


async def run_window(w, end, auto):
    try:
        if auto:
            await queue_auto_updates(w)
        await run_maintenance(w, end)
    except Exception:
        log.exception("Maintenance window %s failed", w["name"])
    finally:
        RUNNING_WINDOWS.discard(w["name"])
        MAINT_CHANGED.set()


async def run_maintenance(w, end):
    batch = "mw-" + secrets.token_hex(3)
    items = [i for i in STATE["scheduled"] if item_window(i) == w["name"]]
    if not items:
        return
    done, skipped = [], []
    log.info("Maintenance window %s: %d update(s)", w["name"], len(items))

    async def per_host(host, its):
        # Docker stacks first, then OS updates; the host itself (may reboot) last
        its.sort(key=lambda i: (i["vmid"] is None, not i.get("stack")))
        for it in its:
            if dt.datetime.now() >= end:
                return  # window over: leave the rest for next time
            STATE["scheduled"] = [x for x in STATE["scheduled"] if x is not it]
            save_state()
            stack = it.get("stack")
            if host not in HOSTS or active_job(host, it["vmid"], stack):
                continue
            if stack:
                pending = (find_stack(host, it["vmid"], stack) or {}).get("updates")
            else:
                pending = len((find_target(host, it["vmid"]) or {}).get("packages") or []) or None
            if not pending:
                skipped.append(it["name"])
                continue
            job = Job("update", host, it["vmid"], it["name"], snapshot=it.get("snapshot"), batch=batch)
            job.quiet, job.reboot, job.stack = True, bool(it.get("reboot")), stack
            JOBS[job.id] = job
            await run_update(job)
            done.append(job)

    by_host = {}
    for it in items:
        by_host.setdefault(it["host"], []).append(it)
    await asyncio.gather(*(per_host(h, its) for h, its in by_host.items()))

    left = [x for x in STATE["scheduled"] if any(x is i for i in items)]
    if DISCORD.get("webhook") and (done or left):
        icon = {"success": "✅", "failed": "❌"}
        lines = [f"🌙 **{w['name']}** maintenance window finished"]
        lines += [f"{icon.get(j.status, '•')} **{j.name}** `{j.host}`: {j.summary}" for j in done]
        if skipped:
            lines.append(f"➖ Already up to date: {', '.join(skipped)}")
        if left:
            lines.append(f"⏭️ Not reached before the window closed, moved to the next one: "
                         f"{', '.join(x['name'] for x in left)}")
        await discord(DISCORD["webhook"], "\n".join(lines))


RUNNING_WINDOWS = set()
AUTO_DONE = set()  # (window name, start) already auto-queued


async def maintenance_loop():
    while True:
        MAINT_CHANGED.clear()
        now = dt.datetime.now()
        wakes = []
        for w in WINDOWS:
            occ = occurrence(w, now)
            if not occ:
                continue
            start, end = occ
            if now < start:
                wakes.append(start)
                continue
            wakes.append(end)
            if w["name"] in RUNNING_WINDOWS:
                continue
            key = (w["name"], start.timestamp())
            wants_auto = key not in AUTO_DONE and any(
                p["auto"] and p["window"] == w["name"] for p in SCHEDULES.values())
            has_items = any(item_window(i) == w["name"] for i in STATE["scheduled"])
            if wants_auto or has_items:
                AUTO_DONE.add(key)
                RUNNING_WINDOWS.add(w["name"])
                spawn(run_window(w, end, wants_auto))
        wake = min(wakes) if wakes else None
        wait = max(1, (wake - now).total_seconds()) if wake else None
        try:
            await asyncio.wait_for(MAINT_CHANGED.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass


@contextlib.asynccontextmanager
async def lifespan(app):
    tasks = [spawn(scheduler()), spawn(maintenance_loop())]
    yield
    for t in tasks:
        t.cancel()


# ---------------------------------------------------------------- web

app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
AUTH = CFG["auth"]


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    if not AUTH.get("disabled"):
        ok = False
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = (secrets.compare_digest(user.encode(), AUTH.get("username", "admin").encode())
                      & secrets.compare_digest(pw.encode(), str(AUTH["password"]).encode()))
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="patchdeck"'})
    # Browsers re-send Basic credentials on cross-site form posts; a custom header blocks that (CSRF).
    if request.method == "POST" and request.headers.get("x-patchdeck") != "1":
        return JSONResponse({"detail": "Missing X-Patchdeck header"}, status_code=403)
    return await call_next(request)


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")


def fill(template, **kw):
    if not template:
        return None
    try:
        return template.format(**kw)
    except (KeyError, IndexError, ValueError):
        return template


@app.get("/api/state")
async def get_state():
    hosts = []
    for name, h in HOSTS.items():
        s = STATE["hosts"].get(name, {})
        node = s.get("node", name)
        default_ct = f"https://{h.address}:8006/?console=lxc&xtermjs=1&vmid={{vmid}}&node={{node}}"
        default_host = {"pve": f"https://{h.address}:8006/?console=shell&xtermjs=1&node={{node}}",
                        "pbs": f"https://{h.address}:8007/"}.get(h.kind)
        containers = []
        for c in s.get("containers", []):
            if c["vmid"] in EXCLUDE:
                continue
            c = dict(c)
            c["console"] = fill(h.console_url or default_ct, vmid=c["vmid"], node=node,
                                name=c["name"], address=h.address, host=name)
            j = active_job(name, c["vmid"])
            c["job"] = j.public() if j else None
            c["scheduled"] = sched_public(name, c["vmid"])
            c["schedule"] = SCHEDULES.get(target_key(name, c["vmid"]))
            c.setdefault("type", "lxc")
            c["stacks"] = deco_stacks(name, c["vmid"], c.get("stacks"))
            containers.append(c)
        hj = active_job(name, None)
        hosts.append({
            "name": name, "address": h.address, "node": node, "kind": h.kind,
            "scheduled": sched_public(name, None),
            "schedule": SCHEDULES.get(target_key(name, None)),
            "host": ({**s["host"], "stacks": deco_stacks(name, None, s["host"].get("stacks"))}
                     if s.get("host") else None),
            "containers": containers,
            "checked_at": s.get("checked_at"), "error": s.get("error"),
            "console": fill(h.host_console_url or default_host, node=node, address=h.address, host=name),
            "job": hj.public() if hj else None,
            "checking": any(j.kind == "check" and j.host == name and j.status in ("queued", "running")
                            for j in JOBS.values()),
        })
    jobs = sorted(JOBS.values(), key=lambda j: j.created, reverse=True)[:25]
    return {"hosts": hosts, "jobs": [j.public() for j in jobs], "next_check": STATE.get("next_check"),
            "snapshot_default": SNAP["enabled"], "snapshot_keep": SNAP["keep"],
            "windows": [window_info(w["name"]) for w in WINDOWS],
            "scheduled_count": len(STATE["scheduled"])}


def deco_stacks(host, vmid, stacks):
    if stacks is None or not DOCKER["enabled"]:
        return None
    out = []
    for st in stacks:
        j = active_job(host, vmid, st["name"])
        out.append({**st, "job": j.public() if j else None,
                    "scheduled": sched_public(host, vmid, st["name"]),
                    "schedule": SCHEDULES.get(target_key(host, vmid, st["name"]))})
    return out


def sched_public(host, vmid, stack=None):
    item = scheduled_item(host, vmid, stack)
    if not item:
        return None
    return {"window": window_info(item_window(item)), "reboot": item.get("reboot", False),
            "snapshot": item.get("snapshot", False), "auto": item.get("auto", False)}


@app.post("/api/schedule")
async def api_schedule(request: Request):
    b = await _body(request)
    vmid = b.get("vmid")
    schedule_target(b.get("host"), int(vmid) if vmid is not None else None,
                    bool(b.get("snapshot")), bool(b.get("reboot")), b.get("window"), b.get("stack") or None)
    return {"ok": True}


@app.post("/api/schedule/cancel")
async def api_schedule_cancel(request: Request):
    b = await _body(request)
    vmid = b.get("vmid")
    unschedule(b.get("host"), int(vmid) if vmid is not None else None, b.get("stack") or None)
    return {"ok": True}


@app.post("/api/check")
async def api_check(request: Request):
    body = await _body(request)
    names = [body["host"]] if body.get("host") else list(HOSTS)
    jobs = []
    for n in names:
        if n not in HOSTS:
            raise HTTPException(404, "Unknown host")
        j = Job("check", n)
        JOBS[j.id] = j
        spawn(run_check(j))
        jobs.append(j.public())
    return {"jobs": jobs}


@app.post("/api/update")
async def api_update(request: Request):
    body = await _body(request)
    vmid = body.get("vmid")
    job = queue_update(body.get("host"), int(vmid) if vmid is not None else None,
                       bool(body.get("snapshot")), reboot=bool(body.get("reboot")), stack=body.get("stack") or None)
    return job.public()


@app.post("/api/update-all")
async def api_update_all(request: Request):
    body = await _body(request)
    # mode "now": update now, except targets with their own maintenance schedule, which go to
    # their window unless "override" is set. mode "schedule": everything goes to a window.
    batch = secrets.token_hex(4)
    snap, reboot = bool(body.get("snapshot")), bool(body.get("reboot"))
    jobs, scheduled = [], 0
    targets = [(h, v, None) for h, v, _, _ in pending_targets()]
    if body.get("include_docker") and DOCKER["enabled"]:
        targets += [(h, v, st) for h, v, st, _, _ in pending_stacks()]
    for host, vmid, stack in targets:
        if vmid is None and not body.get("include_hosts"):
            continue
        ct = find_ct(host, vmid) if vmid is not None else None
        if ct and ct.get("status") != "running":
            continue
        if active_job(host, vmid, stack):
            continue
        has_policy = target_key(host, vmid, stack) in SCHEDULES
        if body.get("mode") == "schedule" or (has_policy and not body.get("override") and WINDOWS):
            window = None if has_policy else body.get("window")
            schedule_target(host, vmid, snap, reboot, window, stack)
            scheduled += 1
        elif not is_scheduled(host, vmid, stack) or body.get("override"):
            jobs.append(queue_update(host, vmid, snap, batch=batch, reboot=reboot, stack=stack).public())
    return {"jobs": jobs, "scheduled": scheduled}


@app.get("/api/jobs/{job_id}")
async def api_job(job_id: str):
    j = JOBS.get(job_id) or _404()
    return dict(j.public(), lines=j.lines)


@app.get("/api/jobs/{job_id}/stream")
async def api_job_stream(job_id: str):
    j = JOBS.get(job_id) or _404()

    async def gen():
        q = asyncio.Queue()
        j.subs.add(q)
        try:
            for line in list(j.lines):
                yield _sse("line", line)
            if j.status not in ("queued", "running"):
                yield _sse("end", j.status)
                return
            while True:
                try:
                    kind, val = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(kind, val)
                if kind == "end":
                    return
        finally:
            j.subs.discard(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _404():
    raise HTTPException(404, "No such job")


async def _body(request):
    try:
        return await request.json()
    except Exception:
        return {}


# ---------------------------------------------------------------- settings

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
ADDR_RE = re.compile(r"^[A-Za-z0-9._:%\-]{1,253}$")
KEY_FILES = {"ssh-ed25519": "ed25519", "ssh-rsa": "rsa", "rsa-sha2-256": "rsa", "rsa-sha2-512": "rsa"}


def host_busy(name):
    return any(j.host == name and j.status in ("queued", "running") for j in JOBS.values())


def get_host(body):
    name = body.get("name")
    if name not in HOSTS:
        raise HTTPException(404, "Unknown host")
    return HOSTS[name]


def bad(msg):
    raise HTTPException(400, msg)


def settings_payload():
    pub = sshsetup.public_key(KEY_FILE)
    nxt = next_run()
    return {
        "hosts": [{
            "name": h.name, "address": h.address, "port": h.port, "user": h.user, "type": h.kind,
            "trusted": sshsetup.is_trusted(KNOWN_HOSTS, h.address, h.port) if KNOWN_HOSTS else None,
            "busy": host_busy(h.name),
            "connected": bool(STATE["hosts"].get(h.name, {}).get("checked_at"))
                         and not STATE["hosts"].get(h.name, {}).get("error"),
        } for h in HOSTS.values()],
        "key": {"path": KEY_FILE, "public": pub,
                "authorize_cmd": sshsetup.authorize_command(pub) if pub else None},
        "host_key_checking": KNOWN_HOSTS is not None,
        "schedule": {"check_times": CFG["schedule"]["check_times"],
                     "days": CFG["schedule"]["days"],
                     "next": nxt.timestamp() if nxt else None},
        "discord": {"webhook_set": bool(DISCORD.get("webhook")),
                    "from_env": bool(os.environ.get("DISCORD_WEBHOOK")),
                    "on_check": DISCORD.get("on_check", "always"),
                    "on_update": bool(DISCORD.get("on_update", True))},
        "snapshots": SNAP,
        "windows_updates": CFG["windows_updates"],
        "docker": {"enabled": DOCKER["enabled"], "prune": DOCKER["prune"],
                   "logins": [{"registry": lg["registry"], "username": lg["username"],
                               "token_set": bool(lg["token"]), "from_env": bool(lg.get("env"))}
                              for lg in DOCKER["logins"]]},
        "windows": [{**w, "next_start": (window_info(w["name"]) or {}).get("start")} for w in WINDOWS],
        "schedules": SCHEDULES,
        "targets": [{"host": n, "kind": h.kind,
                     "stacks": [st["name"] for st in (STATE["hosts"].get(n, {}).get("host") or {}).get("stacks") or []],
                     "containers": [
            {"vmid": c["vmid"], "name": c["name"], "type": c.get("type", "lxc"),
             "stacks": [st["name"] for st in c.get("stacks") or []]}
            for c in STATE["hosts"].get(n, {}).get("containers", []) if c["vmid"] not in EXCLUDE]}
            for n, h in HOSTS.items()],
        "host_kinds": HOST_KINDS,
        "base_url": CFG.get("base_url") or "",
        "exclude": sorted(EXCLUDE),
    }


@app.get("/api/settings")
async def api_settings():
    return settings_payload()


@app.post("/api/settings")
async def api_settings_save(request: Request):
    b = await _body(request)
    if "schedule" in b:
        sc = b["schedule"]
        times = sorted({str(t).strip() for t in sc.get("check_times", [])})
        for t in times:
            if not TIME_RE.match(t):
                bad(f"Invalid time {t!r}; use HH:MM")
        days = sorted({int(d) for d in sc.get("days", [])})
        if any(d < 0 or d > 6 for d in days):
            bad("Invalid day")
        RAW.setdefault("schedule", {})
        RAW["schedule"]["check_times"] = times
        RAW["schedule"]["days"] = days
    if "discord" in b:
        d = b["discord"]
        RAW.setdefault("discord", {})
        if d.get("clear_webhook"):
            RAW["discord"]["webhook"] = ""
        elif d.get("webhook"):
            hook = str(d["webhook"]).strip()
            if not hook.startswith(("https://", "http://")):
                bad("The webhook should be a URL starting with https://")
            RAW["discord"]["webhook"] = hook
        if "on_check" in d:
            if d["on_check"] not in ("always", "new", "off"):
                bad("Invalid notification mode")
            RAW["discord"]["on_check"] = d["on_check"]
        if "on_update" in d:
            RAW["discord"]["on_update"] = bool(d["on_update"])
    if "snapshots" in b:
        sn = b["snapshots"]
        keep = int(sn.get("keep", SNAP["keep"]))
        if not 1 <= keep <= 50:
            bad("Keep must be between 1 and 50")
        if sn.get("on_failure", "abort") not in ("abort", "continue"):
            bad("Invalid snapshot failure option")
        RAW["snapshots"] = {"enabled": bool(sn.get("enabled", True)), "keep": keep,
                            "on_failure": sn.get("on_failure", "abort")}
    if "windows" in b:
        old_names = {w["name"] for w in WINDOWS}
        windows = [{"name": str(w.get("name", "")).strip(), "days": w.get("days", []),
                    "start": str(w.get("start", "")), "end": str(w.get("end", ""))} for w in b["windows"]]
        renames = {str(w.get("was")): str(w.get("name", "")).strip() for w in b["windows"]
                   if w.get("was") and w.get("was") != w.get("name")}
        RAW["maintenance"] = {"windows": windows}
        RAW.pop("maintenance_only", None)
        # keep schedules and queued updates pointing at renamed windows
        scheds = RAW.get("schedules") or dict(SCHEDULES)
        for pol in scheds.values():
            if pol and pol.get("window") in renames:
                pol["window"] = renames[pol["window"]]
        RAW["schedules"] = scheds
        for it in STATE["scheduled"]:
            if it.get("window") in renames:
                it["window"] = renames[it["window"]]
        if old_names:
            save_state()
    if "schedules" in b:
        scheds = {}
        names = {str(w.get("name")) for w in (RAW.get("maintenance") or {}).get("windows", [])} \
            if "windows" in b else {w["name"] for w in WINDOWS}
        for key, pol in (b["schedules"] or {}).items():
            if pol and pol.get("window") in names:
                scheds[str(key)] = {"window": pol["window"], "auto": bool(pol.get("auto")),
                                    "reboot": bool(pol.get("reboot"))}
        RAW["schedules"] = scheds
        RAW.pop("maintenance_only", None)
    if "windows_updates" in b:
        RAW["windows_updates"] = {"drivers": bool(b["windows_updates"].get("drivers"))}
    if "docker" in b:
        d = b["docker"]
        raw = RAW.get("docker") if isinstance(RAW.get("docker"), dict) else {}
        new = {"enabled": bool(d.get("enabled", raw.get("enabled", True))),
               "prune": bool(d.get("prune", raw.get("prune", True))),
               "logins": raw.get("logins") or []}
        if "logins" in d:
            # Tokens are never sent to the browser: an empty token keeps the saved one.
            saved = {registry.normalise_registry(lg.get("registry")): lg for lg in raw.get("logins") or []}
            logins, seen = [], set()
            for lg in d["logins"]:
                reg_name = registry.normalise_registry(lg.get("registry"))
                user = str(lg.get("username") or "").strip()
                if not user:
                    continue
                if reg_name in seen:
                    bad(f"Two logins for {reg_name}")
                seen.add(reg_name)
                token = str(lg.get("token") or "").strip() or (saved.get(reg_name) or {}).get("token", "")
                if not token:
                    bad(f"Enter the password or access token for {reg_name}")
                logins.append({"registry": reg_name, "username": user, "token": token})
            new["logins"] = logins
        RAW["docker"] = new
    if "base_url" in b:
        RAW["base_url"] = str(b["base_url"]).strip()
    if "exclude" in b:
        try:
            RAW["exclude"] = sorted({int(v) for v in b["exclude"]})
        except (TypeError, ValueError):
            bad("Excluded IDs must be numbers")
    try:
        save_config()
    except (ValueError, SystemExit) as e:
        bad(str(e))
    return settings_payload()


@app.post("/api/settings/registry-test")
async def api_registry_test(request: Request):
    b = await _body(request)
    reg_name = registry.normalise_registry(b.get("registry"))
    user = str(b.get("username") or "").strip()
    token = str(b.get("token") or "").strip()
    if not token:  # test the saved token
        token = next((lg["token"] for lg in DOCKER["logins"] if lg["registry"] == reg_name), "")
    if not user or not token:
        bad("Enter a username and token first")
    ok, msg = await registry.test_login(reg_name, user, token)
    return {"ok": ok, "message": msg}


@app.post("/api/settings/discord-test")
async def api_discord_test():
    if not DISCORD.get("webhook"):
        bad("No webhook configured")
    try:
        await discord(DISCORD["webhook"], "👋 Test message from Patchdeck. Notifications are working.", raise_errors=True)
    except Exception as e:
        bad(f"Discord rejected the message: {e}")
    return {"ok": True}


@app.post("/api/hosts")
async def api_host_save(request: Request):
    b = await _body(request)
    name, address = str(b.get("name", "")).strip(), str(b.get("address", "")).strip()
    try:
        port = int(b.get("port") or 22)
    except (TypeError, ValueError):
        bad("Port must be a number")
    if not NAME_RE.match(name):
        bad("Name: letters, numbers, dot, dash or underscore (max 40)")
    if not ADDR_RE.match(address):
        bad("Enter an IP address or hostname")
    if not 1 <= port <= 65535:
        bad("Port must be 1-65535")
    kind = b.get("type") or "pve"
    if kind not in HOST_KINDS:
        bad("Unknown host type")
    hosts = RAW.setdefault("hosts", []) or []
    RAW["hosts"] = hosts
    original = b.get("original")
    if original:
        entry = next((h for h in hosts if h.get("name") == original), None)
        if entry is None:
            raise HTTPException(404, "Unknown host")
        if host_busy(original):
            raise HTTPException(409, "This host has a check or update running")
        if name != original and any(h.get("name") == name for h in hosts):
            raise HTTPException(409, "A host with that name already exists")
        entry.update(name=name, address=address)
        if kind == "pve":
            entry.pop("type", None)
        else:
            entry["type"] = kind
        if port == 22:
            entry.pop("port", None)
        else:
            entry["port"] = port
        if name != original:
            STATE["hosts"].pop(original, None)
            STATE["scheduled"] = [i for i in STATE["scheduled"] if i["host"] != original]
            save_state()
    else:
        if any(h.get("name") == name for h in hosts):
            raise HTTPException(409, "A host with that name already exists")
        hosts.append({"name": name, "address": address, **({"port": port} if port != 22 else {}),
                      **({"type": kind} if kind != "pve" else {})})
    save_config()
    return settings_payload()


@app.post("/api/hosts/delete")
async def api_host_delete(request: Request):
    b = await _body(request)
    h = get_host(b)
    if host_busy(h.name):
        raise HTTPException(409, "This host has a check or update running")
    RAW["hosts"] = [x for x in RAW.get("hosts") or [] if x.get("name") != h.name]
    STATE["hosts"].pop(h.name, None)
    STATE["scheduled"] = [i for i in STATE["scheduled"] if i["host"] != h.name]
    RAW["schedules"] = {k: v for k, v in (RAW.get("schedules") or SCHEDULES).items()
                        if not k.startswith(h.name + "/")}
    save_state()
    save_config()
    return settings_payload()


@app.post("/api/hosts/fingerprint")
async def api_host_fingerprint(request: Request):
    h = get_host(await _body(request))
    try:
        info = sshsetup.describe(await sshsetup.fetch_host_key(h.address, h.port))
    except Exception as e:
        bad(sshsetup.explain(e))
    kind = KEY_FILES.get(info["type"], "ecdsa" if info["type"].startswith("ecdsa") else "ed25519")
    info["verify_cmd"] = f"ssh-keygen -lf /etc/ssh/ssh_host_{kind}_key.pub"
    return info


@app.post("/api/hosts/trust")
async def api_host_trust(request: Request):
    b = await _body(request)
    h = get_host(b)
    if not KNOWN_HOSTS:
        bad("Host key checking is disabled in config.yaml")
    try:
        await sshsetup.trust(KNOWN_HOSTS, h.address, h.port, str(b.get("fingerprint", "")))
    except Exception as e:
        bad(sshsetup.explain(e))
    return settings_payload()


@app.post("/api/hosts/install-key")
async def api_host_install_key(request: Request):
    b = await _body(request)
    h = get_host(b)
    pub = sshsetup.public_key(KEY_FILE)
    if not pub:
        bad("Generate Patchdeck's SSH key first")
    if not b.get("password"):
        bad("Enter the host's root password")
    try:
        await sshsetup.install_key(h.address, h.port, h.user, str(b["password"]), KNOWN_HOSTS, pub)
    except Exception as e:
        bad(sshsetup.explain(e))
    return {"ok": True}


@app.post("/api/hosts/test")
async def api_host_test(request: Request):
    h = get_host(await _body(request))
    try:
        return await sshsetup.test(h.address, h.port, h.user, h.key_file, KNOWN_HOSTS)
    except Exception as e:
        bad(sshsetup.explain(e))


@app.post("/api/key/generate")
async def api_key_generate():
    try:
        sshsetup.generate_key(KEY_FILE)
    except Exception as e:
        bad(str(e))
    return settings_payload()
