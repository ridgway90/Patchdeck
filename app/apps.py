"""Application updates for containers installed with the community-scripts (Proxmox VE Helper-Scripts).

Those containers get an `update` command (/usr/bin/update). It checks the app's GitHub releases and,
if there is a newer one, updates the app in place. It has no check-only mode, so Patchdeck works out
whether an update exists the same way the script does: it reads the installed version the script
recorded (/root/.<app>) and compares it with the app's latest GitHub release. Which repository that
is comes from the app's public script in the community-scripts repository, which is only ever read
as text, never run. Updating then just runs the container's own `update` command.

Everything here is best effort: if Patchdeck can't tell, the app shows "Can't check" and the
update command can still be run by hand.
"""
import asyncio
import os
import re
import shlex
import time

import httpx

RAW_BASE = os.environ.get("PATCHDECK_CS_RAW", "https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct")
GH_API = os.environ.get("PATCHDECK_GH_API", "https://api.github.com")

APP = "@app"  # the "stack" name an app update uses, so it shares scheduling with Docker stacks
# The script may wait for input if something unexpected happens; never let that hang forever.
UPDATE_CMD = "timeout 3600 /usr/bin/update"

# Runs inside the container: the update entrypoint (only to learn the script's name) and every small
# file that could hold a recorded version.
DETECT = r"""
[ -x /usr/bin/update ] || { echo __NOUPDATE__; exit 0; }
echo __UPDATE__
head -c 8000 /usr/bin/update
echo
echo __VERSIONS__
for f in /root/.[A-Za-z0-9]* /opt/*_version.txt; do
  [ -f "$f" ] || continue
  [ -L "$f" ] && continue
  [ "$(wc -c < "$f")" -le 80 ] || continue
  printf '%s\t%s\n' "$f" "$(head -n 1 "$f" | tr -d '\r')"
done
"""

# Plain operating-system containers: their `update` only upgrades OS packages, which Patchdeck's
# normal package check already covers, so they get no app row.
OS_ONLY = {"debian", "ubuntu", "alpine", "devuan"}

NAME_FROM_UPDATE = [
    re.compile(r'UPDATE_SCRIPT_NAME="?([A-Za-z0-9][A-Za-z0-9._-]*)'),
    re.compile(r"/ct/([A-Za-z0-9][A-Za-z0-9._-]*)\.sh"),
]
VERSION_RE = re.compile(r"^v?\d+(?:\.\d+)*(?:[-+._][0-9A-Za-z.]+)*$")
APP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,63}$")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
CALL = re.compile(r"\bcheck_for_gh_release\s+([^\n;&|)]*)")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
SPINNER = re.compile(r"^[⠀-⣿]\s*")


# ---------- scripts whose update is only an apt upgrade

UPDATE_FN = re.compile(r"\bupdate_script\s*\(\)\s*\{")
# The only things such a script may do. Anything else (a version check, a download, a service
# restart, an app's own updater) means the app has its own update and keeps its row.
APT_LINE_OK = [re.compile(p) for p in (
    r"^(header_info|check_container_storage|check_container_resources|exit|fi|else|\})$",
    r"^msg_(info|ok|error|warn)\b.*$",
    r"^(\$STD\s+)?apt_update_safe$",
    r"^(\$STD\s+)?(apt|apt-get)\s+.+$",
    r"^if\s+\[\[.*\]\];?\s*then$",  # "is it installed?" guards
)]
APT_UPGRADES = re.compile(r"apt_update_safe|\bapt(?:-get)?\s+.*\b(upgrade|dist-upgrade|install)\b")


def update_body(text):
    m = UPDATE_FN.search(text)
    if not m:
        return None
    rest = text[m.end():]
    end = re.search(r"^\}\s*$", rest, re.M)  # the function closes with a } at the start of a line
    return rest[:end.start()] if end else rest


def apt_only(text):
    """True when the script's update_script only upgrades packages with apt, which Patchdeck's
    normal package check already covers."""
    body = update_body(text)
    if not body:
        return False
    lines = [ln.strip() for ln in body.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if not lines or not any(APT_UPGRADES.search(ln) for ln in lines):
        return False
    return all(any(rx.match(ln) for rx in APT_LINE_OK) for ln in lines)


# ---------- reading what's inside the container

def parse_detect(out):
    """Name and recorded versions, or None when the guest has no /usr/bin/update (or only the
    plain-OS one)."""
    if "__NOUPDATE__" in out or "__UPDATE__" not in out:
        return None
    body = out.split("__UPDATE__", 1)[1]
    script, _, vers = body.partition("__VERSIONS__")
    name = None
    for rx in NAME_FROM_UPDATE:
        m = rx.search(script)
        if m:
            name = m.group(1)
            break
    if name and name.lower() in OS_ONLY:
        return None
    versions = {}
    for line in vers.splitlines():
        path, sep, val = line.partition("\t")
        val = val.strip()
        if sep and VERSION_RE.match(val) and (path.startswith("/root/.") or path.startswith("/opt/")):
            versions[path] = val
    return {"name": name, "versions": versions}


def installed_version(versions, app):
    """The version the script recorded for `app`: /root/.<app> (current scripts) or the older
    /opt/<app>_version.txt."""
    lc = app.lower().replace(" ", "")
    if f"/root/.{lc}" in versions:
        return versions[f"/root/.{lc}"]
    for path, val in versions.items():
        base = path.rsplit("/", 1)[-1]
        if path.startswith("/opt/") and base.lower().replace(" ", "") == f"{lc}_version.txt":
            return val
    return None


def parse_components(text):
    """Every check_for_gh_release call in an app's script: [{app, repo, note}]. A call that
    passes extra arguments (a pinned version or filter) or builds its arguments at run time can't
    be checked, so it carries a note instead of a repo."""
    out, seen = [], set()
    for m in CALL.finditer(text):
        try:
            args = shlex.split(m.group(1), comments=True)
        except ValueError:
            continue
        args = [a for a in args if a not in ("then", "]]", "}")]
        if len(args) < 2:
            continue
        app, repo = args[0], args[1]
        comp = {"app": app, "repo": repo, "note": None}
        if "$" in app or "$" in repo or not APP_RE.match(app) or not REPO_RE.match(repo):
            comp.update(repo=None, note="the script builds the app or repository name at run time")
        elif len(args) > 2:
            comp.update(note="the script pins or filters versions, which Patchdeck can't follow")
        if (comp["app"], comp["repo"]) not in seen:
            seen.add((comp["app"], comp["repo"]))
            out.append(comp)
    return out


# ---------- looking things up (cached across checks; GitHub allows 60 anonymous requests an hour)

_scripts = {}   # script name -> (fetched_at, text or None, reason)
_releases = {}  # repo -> {"at", "etag", "tag"}
_locks = {}
SCRIPT_TTL, RELEASE_TTL, STALE_OK = 6 * 3600, 900, 6 * 3600


def _lock(key):
    return _locks.setdefault(key, asyncio.Lock())


async def fetch_script(client, name):
    """The app's public script as text, or (None, reason). `name` is only ever used as a file
    name under a fixed address: nothing the container says can make Patchdeck fetch elsewhere."""
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", name or ""):
        return None, "unrecognised script name"
    async with _lock(("script", name)):
        hit = _scripts.get(name)
        if hit and time.time() - hit[0] < (SCRIPT_TTL if hit[1] else 300):
            return hit[1], hit[2]
        try:
            r = await client.get(f"{RAW_BASE}/{name}.sh")
            if r.status_code == 200:
                res = (r.text, None)
            elif r.status_code == 404:
                res = (None, f"no script named {name} in the community-scripts repository")
            else:
                res = (None, f"couldn't read the app's script (HTTP {r.status_code})")
        except httpx.HTTPError as e:
            res = (None, f"couldn't reach the community-scripts repository ({e.__class__.__name__})")
        _scripts[name] = (time.time(), *res)
        return res


async def latest_release(client, repo, token=""):
    """The latest release tag of a GitHub repository, or (None, reason)."""
    async with _lock(("release", repo)):
        hit = _releases.get(repo)
        now = time.time()
        if hit and now - hit["at"] < RELEASE_TTL:
            return hit["tag"], None
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if hit and hit.get("etag"):
            headers["If-None-Match"] = hit["etag"]
        try:
            r = await client.get(f"{GH_API}/repos/{repo}/releases/latest", headers=headers)
        except httpx.HTTPError as e:
            if hit and now - hit["at"] < STALE_OK:
                return hit["tag"], None
            return None, f"couldn't reach GitHub ({e.__class__.__name__})"
        if r.status_code == 304 and hit:
            hit["at"] = now
            return hit["tag"], None
        if r.status_code == 200:
            tag = (r.json() or {}).get("tag_name")
            if tag:
                _releases[repo] = {"at": now, "etag": r.headers.get("etag"), "tag": tag}
                return tag, None
            return None, "the release has no version tag"
        if r.status_code == 404:
            return None, "the repository has no releases (it may use tags instead)"
        if r.status_code in (403, 429):
            return None, "GitHub rate limit reached; add a token under Settings → App updates"
        if r.status_code == 401:
            return None, "GitHub rejected the token"
        return None, f"GitHub answered {r.status_code}"


def _norm(v):
    return (v or "").strip().lstrip("vV")


async def _component(client, comp, versions, token):
    out = {"app": comp["app"], "repo": comp["repo"], "installed": installed_version(versions, comp["app"]),
           "latest": None, "status": "unknown", "note": comp["note"]}
    if comp["note"] or not comp["repo"]:
        return out
    if not out["installed"]:
        out["note"] = "no installed version recorded for it in the container"
        return out
    tag, why = await latest_release(client, comp["repo"], token)
    if not tag:
        out["note"] = why
        return out
    out["latest"] = _norm(tag)
    out["status"] = "current" if _norm(out["installed"]) == out["latest"] else "update"
    return out


async def _annotate_one(client, app, token):
    app.update(components=[], updates=0, status="unknown", note=None)
    name = app.get("name")
    if not name:
        app["note"] = "couldn't tell which community-script installed this"
        return
    text, why = await fetch_script(client, name)
    if text is None:
        app["note"] = why
        return
    comps = parse_components(text)
    if not comps and apt_only(text):
        app["status"] = "os"  # nothing beyond the OS package check; the dashboard hides it
        app["note"] = "this app updates through apt, which the package check already covers"
        return
    if not comps:
        app["note"] = ("this app's script doesn't check GitHub releases, so Patchdeck can't tell whether "
                       "it has an update; run its update to find out")
        return
    app["components"] = list(await asyncio.gather(
        *(_component(client, c, app.get("versions") or {}, token) for c in comps)))
    app["updates"] = sum(1 for c in app["components"] if c["status"] == "update")
    if app["updates"]:
        app["status"] = "update"
    elif all(c["status"] == "current" for c in app["components"]):
        app["status"] = "current"
    else:
        app["note"] = next((c["note"] for c in app["components"] if c["status"] == "unknown" and c["note"]), None)


async def annotate(entries, token=""):
    """Fill in components, updates and status for each guest entry's `app`."""
    todo = [e["app"] for e in entries if e and e.get("app")]
    if not todo:
        return
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "patchdeck"}) as client:
        await asyncio.gather(*(_annotate_one(client, a, token) for a in todo))


async def test_token(token):
    """(ok, message) from GitHub's rate-limit endpoint, which doesn't count against the limit."""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "patchdeck"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{GH_API}/rate_limit", headers=headers)
    except httpx.HTTPError as e:
        return False, f"Couldn't reach GitHub ({e.__class__.__name__})"
    if r.status_code == 401:
        return False, "GitHub rejected the token"
    if r.status_code != 200:
        return False, f"GitHub answered {r.status_code}"
    core = (r.json().get("resources") or {}).get("core") or {}
    left, limit = core.get("remaining"), core.get("limit")
    who = "Token accepted" if token else "No token"
    return True, f"{who}: {left} of {limit} requests left this hour." if left is not None else f"{who}."


# ---------- the update's output

def clean_output(emit):
    """Wrap a line sink so the update's terminal output reads cleanly: colours and cursor codes
    removed, spinner redraws collapsed to their last state, repeats dropped."""
    last = [None]

    def line(raw):
        parts = [p for p in ANSI.sub("", raw).split("\r") if p.strip()]
        text = SPINNER.sub("", parts[-1]).rstrip() if parts else ""
        if text and text != last[0]:
            last[0] = text
            emit(text)
    return line
