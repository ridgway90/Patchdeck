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
from .proxmox import HOST_KINDS, Host, family

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


def effective(raw):
    cfg = copy.deepcopy(raw)
    for key in ("auth", "ssh", "schedule", "discord", "snapshots"):
        if not isinstance(cfg.get(key), dict):
            cfg[key] = {}
    cfg["schedule"].setdefault("check_times", ["08:00"])
    cfg["schedule"].setdefault("days", list(range(7)))
    cfg["exclude"] = cfg.get("exclude") or []
    if not isinstance(cfg.get("maintenance"), dict):
        cfg["maintenance"] = {}
    mw = cfg["maintenance"]
    mw.setdefault("enabled", False)
    mw.setdefault("days", list(range(7)))
    mw.setdefault("start", "02:00")
    mw.setdefault("end", "05:00")
    for k in ("start", "end"):
        if isinstance(mw[k], int):
            mw[k] = f"{mw[k] // 60:02d}:{mw[k] % 60:02d}"
        if not TIME_RE.match(str(mw[k])):
            raise ValueError(f"Invalid maintenance {k} time {mw[k]!r}, use HH:MM")
    cfg["maintenance_only"] = [str(t) for t in cfg.get("maintenance_only") or []]
    cfg["hosts"] = cfg.get("hosts") or []
    if os.environ.get("PATCHDECK_PASSWORD"):
        cfg["auth"]["password"] = os.environ["PATCHDECK_PASSWORD"]
    if os.environ.get("DISCORD_WEBHOOK"):
        cfg["discord"]["webhook"] = os.environ["DISCORD_WEBHOOK"]
    if not cfg["auth"].get("password") and not cfg["auth"].get("disabled"):
        raise SystemExit("Set auth.password in config.yaml (or PATCHDECK_PASSWORD).")
    # YAML reads an unquoted 18:30 as the number 1110 (minutes); turn it back into "18:30".
    cfg["schedule"]["check_times"] = [f"{t // 60:02d}:{t % 60:02d}" if isinstance(t, int) else str(t)
                                      for t in cfg["schedule"]["check_times"] or []]
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
    global SSH, KNOWN_HOSTS, KEY_FILE, HOSTS, SNAP, EXCLUDE, DISCORD, MAINT, MAINT_ONLY
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
    for name in HOSTS:
        HOST_LOCKS.setdefault(name, asyncio.Lock())
    SNAP = {"enabled": bool(CFG["snapshots"].get("enabled", True)),
            "keep": int(CFG["snapshots"].get("keep", 3)),
            "on_failure": CFG["snapshots"].get("on_failure", "abort")}
    EXCLUDE = {int(v) for v in CFG["exclude"]}
    DISCORD = CFG["discord"]
    MAINT = CFG["maintenance"]
    MAINT_ONLY = set(CFG["maintenance_only"])


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
                ("id", "kind", "host", "vmid", "name", "status", "summary", "created", "started", "finished", "batch")}

    @classmethod
    def restore(cls, d):
        j = cls(d["kind"], d["host"], d.get("vmid"), d.get("name"))
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


def active_job(host, vmid):
    return next((j for j in JOBS.values() if j.host == host and j.vmid == vmid
                 and j.kind == "update" and j.status in ("queued", "running")), None)


def find_ct(host, vmid):
    h = STATE["hosts"].get(host) or {}
    return next((c for c in h.get("containers", []) if c["vmid"] == vmid), None)


# ---------------------------------------------------------------- checks

async def run_check(job):
    h = HOSTS[job.host]
    async with HOST_LOCKS[job.host]:
        job.start()
        job.log(f"Connecting to {h.address}…")
        try:
            result = await h.check(exclude=EXCLUDE)
            STATE["hosts"][job.host] = result
            save_state()
            n_ct = sum(len(c["packages"]) for c in result["containers"])
            for c in result["containers"]:
                detail = c["error"] or (c["status"] if c["status"] != "running" else f"{len(c['packages'])} updates")
                job.log(f"  {c['vmid']:>5}  {c['name']:<24} {detail}")
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


async def notify_check():
    mode = DISCORD.get("on_check", "always")
    if mode == "off" or not DISCORD.get("webhook"):
        return
    targets = pending_targets()
    sig = sorted(f"{h}:{v}:{p['name']}:{p['to']}" for h, v, _, pk in targets for p in pk)
    if not targets or (mode == "new" and set(sig) <= set(STATE["notified"])):
        return
    total = sum(len(pk) for *_, pk in targets)
    lines = [f"🔔 **{total} updates available** across {len(targets)} target(s)"]
    for h, vmid, name, pk in targets:
        where = f"`{h}`" if vmid is None else f"`{h}/{vmid}`"
        lines.append(f"• **{name}** {where}: {len(pk)}")
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

async def run_update(job):
    h = HOSTS[job.host]
    async with HOST_LOCKS[job.host]:
        job.start()
        try:
            if job.vmid is None:
                before = len(STATE["hosts"].get(job.host, {}).get("host", {}).get("packages", []))
                job.log(f"$ apt-get dist-upgrade on {job.host}")
                rc = await h.stream(h.upgrade_cmd(), job.log)
            else:
                ct = find_ct(job.host, job.vmid)
                if not ct:
                    raise RuntimeError("Container not in last check; run a check first")
                fam = family(ct["os"])
                if not fam:
                    raise RuntimeError(f"Unsupported OS type: {ct['os']}")
                before = len(ct["packages"])
                if job.snapshot:
                    job.log("Taking snapshot…")
                    try:
                        snap = await h.snapshot(job.vmid)
                        job.log(f"Snapshot created: {snap}")
                    except Exception as e:
                        job.log(f"Snapshot failed: {e}")
                        if SNAP["on_failure"] == "abort":
                            raise RuntimeError("Snapshot failed, update aborted (set snapshots.on_failure: continue to skip)")
                        job.log("Continuing without snapshot (snapshots.on_failure: continue)")
                        job.snapshot = False
                job.log(f"$ {'apt-get dist-upgrade' if fam == 'apt' else 'apk upgrade'} in {ct['name']}")
                rc = await h.stream(h.upgrade_cmd(job.vmid, fam), job.log)
                if rc == 0 and job.snapshot:
                    removed = await h.prune_snapshots(job.vmid, SNAP["keep"])
                    if removed:
                        job.log(f"Removed old snapshots: {', '.join(removed)}")
            job.log("Re-checking…")
            if job.vmid is None:
                STATE["hosts"][job.host]["host"] = await h.check_self()
                after = STATE["hosts"][job.host]["host"]
            else:
                after = await h.check_container(job.vmid)
                cts = STATE["hosts"][job.host]["containers"]
                cts[:] = [after if c["vmid"] == job.vmid else c for c in cts]
            save_state()
            if rc != 0:
                job.finish("failed", f"exit code {rc}")
            else:
                msg = f"{before} → {len(after['packages'])} pending"
                if after.get("reboot"):
                    msg += " · reboot required"
                job.finish("success", msg)
        except Exception as e:
            job.log(f"ERROR: {e}")
            job.finish("failed", str(e))
    await notify_update(job)


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


def queue_update(host, vmid, snapshot, batch=None):
    if host not in HOSTS:
        raise HTTPException(404, "Unknown host")
    if active_job(host, vmid):
        raise HTTPException(409, "An update for this target is already queued or running")
    name = f"{host} (host)" if vmid is None else (find_ct(host, vmid) or {}).get("name", str(vmid))
    job = Job("update", host, vmid, name, snapshot=snapshot and vmid is not None, batch=batch)
    JOBS[job.id] = job
    if is_scheduled(host, vmid):  # updating now replaces a pending maintenance-window update
        STATE["scheduled"] = [i for i in STATE["scheduled"] if not (i["host"] == host and i["vmid"] == vmid)]
        save_state()
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


# ---------------------------------------------------------------- maintenance window

def target_key(host, vmid):
    return f"{host}/{'host' if vmid is None else vmid}"


def is_scheduled(host, vmid):
    return any(i["host"] == host and i["vmid"] == vmid for i in STATE["scheduled"])


def window_at(day):
    """The maintenance window that starts on `day`, as (start, end). End may be the next day."""
    hh, mm = map(int, MAINT["start"].split(":"))
    eh, em = map(int, MAINT["end"].split(":"))
    start = dt.datetime.combine(day, dt.time(hh, mm))
    end = dt.datetime.combine(day, dt.time(eh, em))
    if end <= start:
        end += dt.timedelta(days=1)
    return start, end


def next_window(now=None):
    """The window in progress now, or the next one to start."""
    if not MAINT.get("enabled") or not MAINT.get("days"):
        return None
    now = now or dt.datetime.now()
    days = {int(d) for d in MAINT["days"]}
    for offset in range(-1, 8):  # -1: a window that started yesterday may still be running
        day = (now + dt.timedelta(days=offset)).date()
        if day.weekday() not in days:
            continue
        start, end = window_at(day)
        if end > now:
            return start, end
    return None


def schedule_target(host, vmid, snapshot):
    if host not in HOSTS:
        raise HTTPException(404, "Unknown host")
    if not next_window():
        raise HTTPException(400, "Set up a maintenance window in Settings first")
    if active_job(host, vmid):
        raise HTTPException(409, "An update for this target is already running")
    if not is_scheduled(host, vmid):
        name = f"{host} (host)" if vmid is None else (find_ct(host, vmid) or {}).get("name", str(vmid))
        STATE["scheduled"].append({"host": host, "vmid": vmid, "name": name,
                                   "snapshot": bool(snapshot) and vmid is not None, "added": time.time()})
        save_state()
        MAINT_CHANGED.set()


async def run_maintenance(end):
    batch = "mw-" + secrets.token_hex(3)
    items = list(STATE["scheduled"])
    done, skipped = [], []
    log.info("Maintenance window: %d scheduled update(s)", len(items))

    async def per_host(host, its):
        for it in its:
            if dt.datetime.now() >= end:
                return  # window over: leave the rest for next time
            STATE["scheduled"] = [x for x in STATE["scheduled"] if x is not it]
            save_state()
            if host not in HOSTS or active_job(host, it["vmid"]):
                continue
            pending = (STATE["hosts"].get(host, {}).get("host", {}) if it["vmid"] is None
                       else find_ct(host, it["vmid"]) or {}).get("packages")
            if pending == []:
                skipped.append(it["name"])
                continue
            job = Job("update", host, it["vmid"], it["name"], snapshot=it["snapshot"], batch=batch)
            job.quiet = True
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
        lines = ["🌙 **Maintenance window finished**"]
        lines += [f"{icon.get(j.status, '•')} **{j.name}** `{j.host}`: {j.summary}" for j in done]
        if skipped:
            lines.append(f"➖ Already up to date: {', '.join(skipped)}")
        if left:
            lines.append(f"⏭️ Not reached before the window closed, moved to the next window: "
                         f"{', '.join(x['name'] for x in left)}")
        await discord(DISCORD["webhook"], "\n".join(lines))


async def maintenance_loop():
    while True:
        MAINT_CHANGED.clear()
        win = next_window()
        now = dt.datetime.now()
        if win and win[0] <= now < win[1] and STATE["scheduled"]:
            try:
                await run_maintenance(win[1])
            except Exception:
                log.exception("Maintenance run failed")
            now = dt.datetime.now()
        # Sleep until the window opens or closes, or until settings/the queue change.
        wake = None if not win else (win[0] if now < win[0] else win[1])
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
            c["scheduled"] = is_scheduled(name, c["vmid"])
            c["maint_only"] = target_key(name, c["vmid"]) in MAINT_ONLY
            containers.append(c)
        hj = active_job(name, None)
        hosts.append({
            "name": name, "address": h.address, "node": node, "kind": h.kind,
            "scheduled": is_scheduled(name, None),
            "maint_only": target_key(name, None) in MAINT_ONLY,
            "host": s.get("host"), "containers": containers,
            "checked_at": s.get("checked_at"), "error": s.get("error"),
            "console": fill(h.host_console_url or default_host, node=node, address=h.address, host=name),
            "job": hj.public() if hj else None,
            "checking": any(j.kind == "check" and j.host == name and j.status in ("queued", "running")
                            for j in JOBS.values()),
        })
    jobs = sorted(JOBS.values(), key=lambda j: j.created, reverse=True)[:25]
    win = next_window()
    return {"hosts": hosts, "jobs": [j.public() for j in jobs], "next_check": STATE.get("next_check"),
            "snapshot_default": SNAP["enabled"], "snapshot_keep": SNAP["keep"],
            "maintenance": {"next_start": win[0].timestamp() if win else None,
                            "next_end": win[1].timestamp() if win else None,
                            "scheduled": len(STATE["scheduled"])}}


@app.post("/api/schedule")
async def api_schedule(request: Request):
    b = await _body(request)
    vmid = b.get("vmid")
    schedule_target(b.get("host"), int(vmid) if vmid is not None else None, bool(b.get("snapshot")))
    return {"ok": True}


@app.post("/api/schedule/cancel")
async def api_schedule_cancel(request: Request):
    b = await _body(request)
    vmid = b.get("vmid")
    vmid = int(vmid) if vmid is not None else None
    STATE["scheduled"] = [i for i in STATE["scheduled"] if not (i["host"] == b.get("host") and i["vmid"] == vmid)]
    save_state()
    MAINT_CHANGED.set()
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
    job = queue_update(body.get("host"), int(vmid) if vmid is not None else None, bool(body.get("snapshot")))
    return job.public()


@app.post("/api/update-all")
async def api_update_all(request: Request):
    body = await _body(request)
    batch = secrets.token_hex(4)
    jobs, scheduled = [], 0
    for host, vmid, _, _ in pending_targets():
        if vmid is None and not body.get("include_hosts"):
            continue
        if active_job(host, vmid) or is_scheduled(host, vmid):
            continue
        if target_key(host, vmid) in MAINT_ONLY and next_window():
            schedule_target(host, vmid, bool(body.get("snapshot")))
            scheduled += 1
            continue
        jobs.append(queue_update(host, vmid, bool(body.get("snapshot")), batch=batch).public())
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
    win = next_window()
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
        "maintenance": {**MAINT, "next_start": win[0].timestamp() if win else None,
                        "next_end": win[1].timestamp() if win else None},
        "maintenance_only": sorted(MAINT_ONLY),
        "targets": [{"host": n, "kind": h.kind, "containers": [
            {"vmid": c["vmid"], "name": c["name"]}
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
    if "maintenance" in b:
        m = b["maintenance"]
        for k in ("start", "end"):
            if not TIME_RE.match(str(m.get(k, ""))):
                bad(f"Invalid {k} time; use HH:MM")
        if m["start"] == m["end"]:
            bad("Start and end can't be the same time")
        days = sorted({int(d) for d in m.get("days", [])})
        if any(d < 0 or d > 6 for d in days):
            bad("Invalid day")
        RAW["maintenance"] = {"enabled": bool(m.get("enabled")), "days": days,
                              "start": m["start"], "end": m["end"]}
    if "maintenance_only" in b:
        RAW["maintenance_only"] = sorted({str(t) for t in b["maintenance_only"]})
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
    RAW["maintenance_only"] = [t for t in RAW.get("maintenance_only") or [] if not t.startswith(h.name + "/")]
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
