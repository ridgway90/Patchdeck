"""Patchdeck: a small update dashboard for Proxmox hosts and their LXC containers."""
import asyncio
import base64
import contextlib
import datetime as dt
import json
import logging
import os
import secrets
import time
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .notify import discord
from .proxmox import Host, family

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("patchdeck")
logging.getLogger("asyncssh").setLevel(logging.WARNING)

CONFIG_PATH = Path(os.environ.get("PATCHDECK_CONFIG", "/data/config.yaml"))
DATA_DIR = Path(os.environ.get("PATCHDECK_DATA", CONFIG_PATH.parent))
STATIC = Path(__file__).parent / "static"


# ---------------------------------------------------------------- config

def load_config():
    cfg = yaml.safe_load(CONFIG_PATH.read_text()) or {}
    cfg.setdefault("auth", {})
    cfg.setdefault("ssh", {})
    cfg.setdefault("schedule", {}).setdefault("check_times", ["08:00"])
    cfg.setdefault("discord", {})
    cfg.setdefault("snapshots", {})
    cfg.setdefault("exclude", [])
    if os.environ.get("PATCHDECK_PASSWORD"):
        cfg["auth"]["password"] = os.environ["PATCHDECK_PASSWORD"]
    if os.environ.get("DISCORD_WEBHOOK"):
        cfg["discord"]["webhook"] = os.environ["DISCORD_WEBHOOK"]
    if not cfg["auth"].get("password") and not cfg["auth"].get("disabled"):
        raise SystemExit("Set auth.password in config.yaml (or PATCHDECK_PASSWORD).")
    if not cfg.get("hosts"):
        raise SystemExit("Add at least one entry under hosts: in config.yaml.")
    return cfg


CFG = load_config()
SSH = CFG["ssh"]
KNOWN_HOSTS = SSH.get("known_hosts", str(DATA_DIR / "known_hosts"))
if KNOWN_HOSTS in ("none", "off", False):
    log.warning("SSH host key checking is DISABLED (ssh.known_hosts: none)")
    KNOWN_HOSTS = None

HOSTS = {
    h["name"]: Host(
        name=h["name"], address=h["address"], port=h.get("port", 22),
        user=h.get("user", SSH.get("user", "root")),
        key_file=h.get("key_file", SSH.get("key_file", str(DATA_DIR / "id_ed25519"))),
        known_hosts=KNOWN_HOSTS,
        console_url=h.get("console_url", CFG.get("console_url")),
        host_console_url=h.get("host_console_url", CFG.get("host_console_url")),
    )
    for h in CFG["hosts"]
}
SNAP = {"enabled": CFG["snapshots"].get("enabled", True),
        "keep": int(CFG["snapshots"].get("keep", 3)),
        "on_failure": CFG["snapshots"].get("on_failure", "abort")}
EXCLUDE = {int(v) for v in CFG["exclude"]}
DISCORD = CFG["discord"]


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
HOST_LOCKS = {name: asyncio.Lock() for name in HOSTS}
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
    if not DISCORD.get("on_update", True) or not DISCORD.get("webhook"):
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
    spawn(run_update(job))
    return job


# ---------------------------------------------------------------- scheduler

async def scheduler():
    times = [tuple(map(int, t.split(":"))) for t in CFG["schedule"]["check_times"]]
    if not STATE["hosts"]:
        await check_all(auto=False)
    while True:
        now = dt.datetime.now()
        candidates = []
        for hh, mm in times:
            c = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            candidates.append(c if c > now else c + dt.timedelta(days=1))
        nxt = min(candidates)
        STATE["next_check"] = nxt.timestamp()
        await asyncio.sleep(max(1, (nxt - now).total_seconds()))
        try:
            await check_all(auto=True)
        except Exception:
            log.exception("Scheduled check failed")


@contextlib.asynccontextmanager
async def lifespan(app):
    t = spawn(scheduler())
    yield
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
        default_host = f"https://{h.address}:8006/?console=shell&xtermjs=1&node={{node}}"
        containers = []
        for c in s.get("containers", []):
            c = dict(c)
            c["console"] = fill(h.console_url or default_ct, vmid=c["vmid"], node=node,
                                name=c["name"], address=h.address, host=name)
            j = active_job(name, c["vmid"])
            c["job"] = j.public() if j else None
            containers.append(c)
        hj = active_job(name, None)
        hosts.append({
            "name": name, "address": h.address, "node": node,
            "host": s.get("host"), "containers": containers,
            "checked_at": s.get("checked_at"), "error": s.get("error"),
            "console": fill(h.host_console_url or default_host, node=node, address=h.address, host=name),
            "job": hj.public() if hj else None,
            "checking": any(j.kind == "check" and j.host == name and j.status in ("queued", "running")
                            for j in JOBS.values()),
        })
    jobs = sorted(JOBS.values(), key=lambda j: j.created, reverse=True)[:25]
    return {"hosts": hosts, "jobs": [j.public() for j in jobs], "next_check": STATE.get("next_check"),
            "snapshot_default": SNAP["enabled"], "snapshot_keep": SNAP["keep"]}


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
    jobs = []
    for host, vmid, _, _ in pending_targets():
        if vmid is None and not body.get("include_hosts"):
            continue
        if active_job(host, vmid):
            continue
        jobs.append(queue_update(host, vmid, bool(body.get("snapshot")), batch=batch).public())
    return {"jobs": jobs}


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
