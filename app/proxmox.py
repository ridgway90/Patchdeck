"""Talks to a Proxmox VE host over SSH and runs commands on it and inside its LXCs."""
import asyncio
import json
import os
import re
import secrets
import shlex
import time

import asyncssh

from . import apps, windows

APT_FAMILY = {"debian", "ubuntu", "devuan", "raspbian", "linuxmint", "pop", "kali"}
APK_FAMILY = {"alpine"}

# Lists pending updates. Prints __UPDATE_FAILED__ if the package index could not be refreshed.
APT_LOCK = "-o DPkg::Lock::Timeout=300"
APT_LIST = (
    "apt-get -o DPkg::Lock::Timeout=60 update -qq >/dev/null 2>&1 || echo __UPDATE_FAILED__; "
    "apt list --upgradable 2>/dev/null"
)
APK_LIST = (
    "apk update -q >/dev/null 2>&1 || echo __UPDATE_FAILED__; "
    "apk version -l '<' 2>/dev/null"
)
APT_UPGRADE = (
    "export DEBIAN_FRONTEND=noninteractive; "
    f"apt-get {APT_LOCK} update && "
    f"apt-get {APT_LOCK} -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold dist-upgrade && "
    f"apt-get {APT_LOCK} -y autoremove"
)
APK_UPGRADE = "apk update && apk upgrade"
CT_REBOOT = "[ -f /var/run/reboot-required ] && echo yes || echo no"
# A PVE host needs a reboot if a package asked for one, or a newer kernel is installed than the running one.
HOST_REBOOT = (
    "running=$(uname -r); "
    "newest=$(ls /boot/vmlinuz-* 2>/dev/null | sed 's|/boot/vmlinuz-||' | sort -V | tail -n 1); "
    "{ [ -f /var/run/reboot-required ] || { [ -n \"$newest\" ] && [ \"$running\" != \"$newest\" ]; }; } "
    "&& echo yes || echo no"
)

APT_LINE = re.compile(r"^(\S+?)/\S+\s+(\S+)\s+\S+\s+\[upgradable from: ([^\]]+)\]")
APK_LINE = re.compile(r"^(\S+)-(\d[^\s-]*(?:-r\d+)?)\s+<\s+(\S+)")
SNAP_NAME = re.compile(r"(pre-update-\d{8}-\d{6})")
RC_MARK = "__PATCHDECK_RC__"


DOCKER_ENV = 'export HOME="${HOME:-/root}"; '
DOCKER_LS = (DOCKER_ENV + "command -v docker >/dev/null 2>&1 || { echo __NODOCKER__; exit 0; }; "
             "docker compose version >/dev/null 2>&1 || { echo __NOCOMPOSE__; exit 0; }; "
             "docker compose ls --all --format json")


def compose_args(name, files):
    args = ["-p", shlex.quote(name)]
    for f in files:
        args += ["-f", shlex.quote(f)]
    return " ".join(args)


def images_script(stacks):
    """For each stack: its image refs, and the registry digests of the local copies."""
    parts = []
    for st in stacks:
        parts.append(
            f"echo {shlex.quote('__STACK__ ' + st['name'])}; "
            f"docker compose {compose_args(st['name'], st['files'])} config --images 2>/dev/null | sort -u | "
            "while read -r img; do echo \"__IMG__ $img\"; "
            "docker image inspect --format '{{json .RepoDigests}}' \"$img\" 2>/dev/null || echo __MISSING__; done")
    return DOCKER_ENV + "; ".join(parts)


def parse_images(out, stacks):
    by_name = {st["name"]: st for st in stacks}
    cur, img = None, None
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("__STACK__ "):
            cur, img = by_name.get(line[10:]), None
        elif line.startswith("__IMG__ ") and cur is not None:
            img = {"ref": line[8:], "digests": [], "missing": False}
            cur["images"].append(img)
        elif img is not None and line == "__MISSING__":
            img["missing"] = True
        elif img is not None and line.startswith("["):
            try:
                img["digests"] = [d.split("@", 1)[1] for d in json.loads(line) if "@" in d]
            except ValueError:
                pass


def stack_update_script(name, files, prune=True):
    args = compose_args(name, files)
    script = (DOCKER_ENV + f"cd {shlex.quote(os.path.dirname(files[0]) or '/')} && "
              f"docker compose --ansi never {args} pull && docker compose --ansi never {args} up -d")
    if prune:
        script += " && docker image prune -f"
    return script


def family(ostype):
    if ostype == "mswindows":
        return "win"
    if ostype in APT_FAMILY:
        return "apt"
    if ostype in APK_FAMILY:
        return "apk"
    return None


def parse_packages(fam, out):
    pkgs = []
    for line in out.splitlines():
        m = (APT_LINE if fam == "apt" else APK_LINE).match(line.strip())
        if not m:
            continue
        if fam == "apt":
            pkgs.append({"name": m.group(1), "from": m.group(3), "to": m.group(2)})
        else:
            pkgs.append({"name": m.group(1), "from": m.group(2), "to": m.group(3)})
    return pkgs


def ct_cmd(vmid, inner):
    """Run a shell snippet inside an LXC via pct exec."""
    return f"pct exec {int(vmid)} -- sh -c {shlex.quote(inner)}"


HOST_KINDS = {"pve": "Proxmox VE", "pbs": "Proxmox Backup Server", "debian": "Debian/Ubuntu server"}


class Host:
    def __init__(self, name, address, port=22, user="root", key_file=None,
                 known_hosts=None, console_url=None, host_console_url=None, kind="pve"):
        self.name = name
        self.kind = kind if kind in HOST_KINDS else "pve"
        self.win_drivers = False  # include driver updates for Windows VMs
        self.address = address
        self.port = port
        self.user = user
        self.key_file = key_file
        self.known_hosts = known_hosts
        self.console_url = console_url
        self.host_console_url = host_console_url

    def _connect(self):
        if self.known_hosts and not os.path.exists(self.known_hosts):
            raise RuntimeError("Host fingerprint not trusted yet; set it up in Settings")
        return asyncssh.connect(
            self.address, port=self.port, username=self.user,
            client_keys=[self.key_file] if self.key_file else None,
            known_hosts=self.known_hosts, connect_timeout=15,
            keepalive_interval=30,
        )

    @staticmethod
    async def _run(conn, cmd, timeout=300, check=True):
        try:
            r = await conn.run(cmd, check=False, timeout=timeout)
        except asyncssh.TimeoutError:
            raise RuntimeError(f"`{cmd.split(' --')[0][:60]}` timed out after {int(timeout)}s") from None
        if check and r.exit_status != 0:
            err = (r.stderr or r.stdout or "").strip().splitlines()
            raise RuntimeError(f"`{cmd.split(' --')[0][:60]}` failed: {err[-1] if err else r.exit_status}")
        return r.stdout or ""

    # ---------- running things in a target (host, container or VM) ----------

    async def _exec(self, conn, kind, vmid, script, timeout=300):
        if kind == "vm":
            _, out, _ = await self._guest_exec(conn, vmid, script, timeout)
            return out
        return await self._run(conn, ct_cmd(vmid, script) if kind == "lxc" else script, timeout=timeout)

    async def run_in(self, kind, vmid, script, on_line):
        """Run a long command in a target, streaming its output. Returns the exit code."""
        if kind == "vm":
            return await self.run_vm(vmid, script, on_line)
        return await self.stream(ct_cmd(vmid, script) if kind == "lxc" else script, on_line)

    async def _docker(self, conn, kind, vmid):
        """Compose stacks in a target with their images, or None when Docker isn't installed."""
        out = await self._exec(conn, kind, vmid, DOCKER_LS, 60)
        if "__NODOCKER__" in out:
            return None
        if "__NOCOMPOSE__" in out:
            return {"stacks": [], "error": "Docker is installed but the Compose plugin isn't (docker compose)"}
        listed = json.loads(out.strip() or "[]")
        stacks = [{"name": st["Name"], "status": st.get("Status", ""),
                   "files": [f for f in st.get("ConfigFiles", "").split(",") if f], "images": []}
                  for st in listed]
        stacks = [st for st in stacks if st["files"]]
        if stacks:
            parse_images(await self._exec(conn, kind, vmid, images_script(stacks), 180), stacks)
        return {"stacks": stacks, "error": None}

    async def _add_docker(self, conn, entry, kind, vmid):
        try:
            d = await self._docker(conn, kind, vmid)
        except Exception as e:
            entry["stacks"], entry["docker_error"] = [], f"Couldn't read Docker stacks: {e}"
            return
        entry["stacks"] = d["stacks"] if d else None
        entry["docker_error"] = d["error"] if d else None

    async def _add_app(self, conn, entry, vmid):
        """Note the community-scripts app in a container, if it has one (/usr/bin/update)."""
        try:
            entry["app"] = apps.parse_detect(await self._exec(conn, "lxc", vmid, apps.DETECT, 30))
        except Exception:
            entry["app"] = None  # not worth failing the whole check over

    async def check_app(self, vmid):
        entry = {}
        async with self._connect() as conn:
            await self._add_app(conn, entry, vmid)
        return entry

    async def check_docker(self, kind, vmid):
        entry = {}
        async with self._connect() as conn:
            await self._add_docker(conn, entry, kind, vmid)
        return entry

    # ---------- checks ----------

    async def _check_self(self, conn):
        out = await self._run(conn, f"{APT_LIST}; echo __REBOOT__; {HOST_REBOOT}")
        listing, _, reboot = out.partition("__REBOOT__")
        return {
            "packages": parse_packages("apt", listing),
            "reboot": reboot.strip() == "yes",
            "warning": "Could not refresh package lists" if "__UPDATE_FAILED__" in listing else None,
            "checked_at": time.time(),
        }

    async def _ct_config(self, conn, vmid):
        out = await self._run(conn, f"pct config {int(vmid)}", timeout=30)
        cfg = {}
        for line in out.splitlines():
            k, sep, v = line.partition(":")
            if sep:
                cfg[k.strip()] = v.strip()
        return cfg

    @staticmethod
    def _entry(g, kind):
        return {"vmid": int(g["vmid"]), "type": kind, "name": g.get("name") or str(g["vmid"]),
                "status": g.get("status"), "os": None, "packages": [], "reboot": False,
                "error": None, "warning": None, "checked_at": time.time(),
                "stacks": None, "docker_error": None, "unsupported": False, "app": None}

    @staticmethod
    def _fill(entry, fam, out):
        listing, _, reboot = out.partition("__REBOOT__")
        entry["packages"] = parse_packages(fam, listing)
        entry["reboot"] = reboot.strip() == "yes"
        if "__UPDATE_FAILED__" in listing:
            entry["warning"] = "Could not refresh package lists"

    async def _check_ct(self, conn, ct):
        entry = self._entry(ct, "lxc")
        vmid = entry["vmid"]
        try:
            cfg = await self._ct_config(conn, vmid)
            entry["os"] = cfg.get("ostype")
            entry["name"] = ct.get("name") or cfg.get("hostname") or str(vmid)
            if entry["status"] != "running":
                return entry
            fam = family(entry["os"])
            if not fam:
                entry["error"], entry["unsupported"] = f"Unsupported OS type: {entry['os']}", True
                return entry
            listing_cmd = APT_LIST if fam == "apt" else APK_LIST
            self._fill(entry, fam, await self._run(conn, ct_cmd(vmid, f"{listing_cmd}; echo __REBOOT__; {CT_REBOOT}")))
            await self._add_docker(conn, entry, "lxc", vmid)
            await self._add_app(conn, entry, vmid)
        except Exception as e:  # one broken container should not fail the whole host
            entry["error"] = str(e)
        return entry

    # VMs are reached through the QEMU guest agent (qm guest exec), so they need
    # qemu-guest-agent installed and the agent option enabled on the VM.

    async def _guest_exec(self, conn, vmid, inner, timeout=180):
        out = await self._run(conn, f"qm guest exec {int(vmid)} --timeout {timeout} -- sh -c {shlex.quote(inner)}",
                              timeout=timeout + 30)
        d = json.loads(out)
        if not d.get("exited"):
            raise RuntimeError("Command inside the VM did not finish in time")
        return int(d.get("exitcode", 1)), d.get("out-data", ""), d.get("err-data", "")

    async def _ps(self, conn, vmid, script, timeout=300):
        """Run a PowerShell script in a Windows VM through the guest agent."""
        out = await self._run(conn, windows.ps_command(vmid, script, timeout), timeout=timeout + 30)
        d = json.loads(out)
        if not d.get("exited"):
            raise RuntimeError("PowerShell inside the VM did not finish in time")
        return int(d.get("exitcode", 1)), d.get("out-data", ""), d.get("err-data", "")

    async def run_windows(self, vmid, on_line, max_hours=6):
        """Install Windows updates via a SYSTEM scheduled task and follow its log."""
        tag = windows.new_tag()
        start, log = windows.start_script(tag, self.win_drivers)
        conn = await self._connect()
        try:
            rc, out, err = await self._ps(conn, vmid, start, 120)
            if rc != 0 or "started" not in out:
                raise RuntimeError(f"Could not start Windows Update in the VM: {(err or out).strip()[-300:]}")
            offset, buf, fails, deadline = 0, "", 0, time.time() + max_hours * 3600
            while time.time() < deadline:
                await asyncio.sleep(5)
                try:
                    if conn is None:
                        conn = await self._connect()
                    _, out, _ = await self._ps(conn, vmid, windows.poll_script(log, offset), 60)
                    fails = 0
                except Exception:
                    fails += 1
                    if conn is not None:
                        conn.close()
                        conn = None
                    if fails > 36:
                        raise RuntimeError(f"Lost contact with the VM's guest agent; Windows Update may still be running. Log: {log}")
                    continue
                head, _, data = out.partition("\n")
                head = head.strip().lstrip("\ufeff")
                if head.startswith("__LEN__"):
                    offset = int(head[7:] or offset)
                buf += data
                *lines, buf = buf.split("\n")
                for line in lines:
                    line = line.rstrip("\r").lstrip("\ufeff")
                    if line.startswith(windows.RC_MARK):
                        try:
                            await self._ps(conn, vmid, windows.cleanup_script(tag), 60)
                        except Exception:
                            pass
                        return int(line[len(windows.RC_MARK):] or 1)
                    if line:
                        on_line(line)
            raise RuntimeError(f"Windows Update is still running after {max_hours} hours. Log: {log}")
        finally:
            if conn is not None:
                conn.close()

    async def _agent_ok(self, conn, vmid):
        r = await conn.run(f"qm guest cmd {int(vmid)} ping", check=False, timeout=25)
        return r.exit_status == 0

    async def _check_vm(self, conn, vm):
        entry = self._entry(vm, "vm")
        vmid = entry["vmid"]
        if entry["status"] != "running":
            return entry
        try:
            if not await self._agent_ok(conn, vmid):
                entry["error"] = "Guest agent not responding: install qemu-guest-agent in the VM and enable it in the VM's Options"
                return entry
            info = json.loads(await self._run(conn, f"qm guest cmd {vmid} get-osinfo", timeout=25))
            entry["os"] = info.get("id") or "unknown"
            entry["os_name"] = info.get("pretty-name")
            fam = family(entry["os"])
            if fam == "win":
                rc, out, err = await self._ps(conn, vmid, windows.CHECK.replace(
                    "__CRITERIA__", windows.criteria(self.win_drivers)), 900)
                if rc != 0:
                    raise RuntimeError(f"Windows Update search failed: {(err or out).strip()[-300:]}")
                entry["packages"], entry["reboot"], os_name = windows.parse_check(out)
                entry["os_name"] = os_name or entry["os_name"]
                return entry
            if not fam:
                entry["error"], entry["unsupported"] = f"Unsupported OS: {info.get('pretty-name') or entry['os']}", True
                return entry
            listing_cmd = APT_LIST if fam == "apt" else APK_LIST
            _, out, _ = await self._guest_exec(conn, vmid, f"{listing_cmd}; echo __REBOOT__; {CT_REBOOT}")
            self._fill(entry, fam, out)
            await self._add_docker(conn, entry, "vm", vmid)
        except Exception as e:
            entry["error"] = str(e)
        return entry

    async def _guests(self, conn):
        node = (await self._run(conn, "hostname", timeout=15)).strip()
        q = shlex.quote(node)
        lxc = json.loads(await self._run(conn, f"pvesh get /nodes/{q}/lxc --output-format json", timeout=60))
        qemu = json.loads(await self._run(conn, f"pvesh get /nodes/{q}/qemu --output-format json", timeout=60))
        qemu = [v for v in qemu if not v.get("template")]
        return node, [(g, "lxc") for g in lxc] + [(g, "vm") for g in qemu]

    async def _check_guest(self, conn, g, kind):
        return await (self._check_ct(conn, g) if kind == "lxc" else self._check_vm(conn, g))

    async def check(self, exclude=()):
        """Full check: host packages plus every LXC and VM on the node."""
        async with self._connect() as conn:
            if self.kind != "pve":
                node = (await self._run(conn, "hostname", timeout=15)).strip()
                host_info = await self._check_self(conn)
                if self.kind == "debian":
                    await self._add_docker(conn, host_info, "host", None)
                return {"node": node, "host": host_info, "containers": [],
                        "checked_at": time.time(), "error": None}
            host_info = await self._check_self(conn)
            node, guests = await self._guests(conn)
            sem = asyncio.Semaphore(4)

            async def one(g, kind):
                async with sem:
                    return await self._check_guest(conn, g, kind)

            results = await asyncio.gather(*(one(g, k) for g, k in guests if int(g["vmid"]) not in exclude))
        return {"node": node, "host": host_info, "containers": sorted(results, key=lambda e: e["vmid"]),
                "checked_at": time.time(), "error": None}

    async def check_guest(self, vmid):
        async with self._connect() as conn:
            _, guests = await self._guests(conn)
            match = next(((g, k) for g, k in guests if int(g["vmid"]) == int(vmid)), None)
            if match is None:
                raise RuntimeError(f"Guest {vmid} no longer exists")
            return await self._check_guest(conn, *match)

    async def check_self(self):
        async with self._connect() as conn:
            return await self._check_self(conn)

    # ---------- actions ----------

    async def stream(self, cmd, on_line):
        """Run cmd detached on the host and stream its output. Returns the command's exit code.

        The command keeps running if the SSH connection drops (or Patchdeck's network blips);
        the log is then followed again from where it stopped.
        """
        log = f"/tmp/patchdeck-{secrets.token_hex(6)}.log"
        inner = f"( {cmd} ); echo {RC_MARK}$?"  # subshell: the marker is written even if cmd exits
        # The log is created before the job starts so following it can never race its creation.
        start = f": > {log}; setsid nohup sh -c {shlex.quote(inner)} >> {log} 2>&1 < /dev/null & echo $!"
        async with self._connect() as conn:
            pid = int((await self._run(conn, start, timeout=30)).strip())
        offset, buf, rc, fails = 0, b"", None, 0
        mark = RC_MARK.encode()
        while rc is None:
            try:
                async with self._connect() as conn:
                    follow = f"tail -c +{offset + 1} -F --pid={pid} {log} 2>/dev/null"
                    async with conn.create_process(follow, encoding=None) as proc:
                        while True:
                            chunk = await proc.stdout.read(65536)
                            if not chunk:
                                break
                            fails = 0
                            offset += len(chunk)
                            buf += chunk
                            *lines, buf = buf.split(b"\n")
                            for raw in lines:
                                if raw.startswith(mark):
                                    rc = int(raw[len(mark):].strip() or 1)
                                else:
                                    on_line(raw.decode("utf-8", "replace").rstrip("\r"))
                        await proc.wait()
                        finished = proc.exit_status is not None  # None: the connection went away
                    if rc is not None:
                        await conn.run(f"rm -f {log}", check=False, timeout=30)
                    elif finished:
                        break  # the job ended without reporting an exit code
            except (OSError, asyncssh.Error, asyncio.TimeoutError) as e:
                finished = False
            if rc is None and not finished:
                fails += 1
                if fails > 20:
                    break
                if fails == 1:
                    on_line("(connection to the host lost; the update keeps running, reconnecting…)")
                await asyncio.sleep(min(30, 3 * fails))
        if rc is None:
            raise RuntimeError(f"Lost track of the update before it finished. It may still be running on "
                               f"the host; its output is in {log}. Run a check again in a few minutes.")
        return rc

    async def run_vm(self, vmid, inner, on_line, max_hours=4):
        """Run a command inside a VM through the guest agent and follow its log.

        The command is started with systemd-run (or setsid when there's no systemd) so it isn't
        killed if the upgrade restarts qemu-guest-agent itself, then the log is polled.
        """
        tag = secrets.token_hex(6)
        log = f"/var/tmp/patchdeck-{tag}.log"
        body = f"( {inner} ) >> {log} 2>&1; echo {RC_MARK}$? >> {log}"
        start = (f": > {log}; if command -v systemd-run >/dev/null 2>&1; then "
                 f"systemd-run --unit=patchdeck-{tag} --collect --quiet sh -c {shlex.quote(body)}; "
                 f"else setsid nohup sh -c {shlex.quote(body)} >/dev/null 2>&1 < /dev/null & fi")
        conn = await self._connect()
        try:
            rc, out, err = await self._guest_exec(conn, vmid, start, 60)
            if rc != 0:
                raise RuntimeError(f"Could not start the update in the VM: {(err or out).strip()}")
            offset, buf, fails, deadline = 0, "", 0, time.time() + max_hours * 3600
            while time.time() < deadline:
                await asyncio.sleep(3)
                try:
                    if conn is None:
                        conn = await self._connect()
                    _, out, _ = await self._guest_exec(conn, vmid, f"tail -c +{offset + 1} {log}", 60)
                    fails = 0
                except Exception:
                    fails += 1  # the agent restarts while it upgrades itself; keep trying for a while
                    if conn is not None:
                        conn.close()
                        conn = None
                    if fails > 40:
                        raise RuntimeError(f"Lost contact with the VM's guest agent; the update may still be running. Log: {log}")
                    continue
                offset += len(out.encode())
                buf += out
                *lines, buf = buf.split("\n")
                for line in lines:
                    if line.startswith(RC_MARK):
                        try:
                            await self._guest_exec(conn, vmid, f"rm -f {log}", 30)
                        except Exception:
                            pass
                        return int(line[len(RC_MARK):] or 1)
                    on_line(line)
            raise RuntimeError(f"The update in the VM is still running after {max_hours} hours. Log: {log}")
        finally:
            if conn is not None:
                conn.close()

    async def reboot_guest(self, vmid, kind, on_line, wait=300):
        tool = "pct" if kind == "lxc" else "qm"
        async with self._connect() as conn:
            # Windows can spend a long time "working on updates" while shutting down
            await self._run(conn, f"{tool} reboot {int(vmid)} --timeout {max(300, wait)}", timeout=max(300, wait) + 120)
            on_line("Reboot command sent, waiting for it to come back…")
            end = time.time() + wait
            while time.time() < end:
                await asyncio.sleep(5)
                status = await conn.run(f"{tool} status {int(vmid)}", check=False, timeout=30)
                if "running" not in (status.stdout or ""):
                    continue
                if kind == "lxc" or await self._agent_ok(conn, vmid):
                    on_line("Back up.")
                    return True
        on_line(f"Still not back after {wait // 60} minutes; check it in Proxmox.")
        return False

    async def reboot_self(self):
        async with self._connect() as conn:
            await self._run(conn, "shutdown -r +1 'Patchdeck: rebooting after updates'", timeout=30)

    def upgrade_cmd(self, vmid=None, fam="apt"):
        if vmid is None:
            return APT_UPGRADE
        return ct_cmd(vmid, APT_UPGRADE if fam == "apt" else APK_UPGRADE)

    async def snapshot(self, vmid, kind="lxc"):
        tool = "pct" if kind == "lxc" else "qm"
        name = time.strftime("pre-update-%Y%m%d-%H%M%S")
        async with self._connect() as conn:
            await self._run(conn, f"{tool} snapshot {int(vmid)} {name} --description 'Taken by patchdeck before updating'", timeout=900)
        return name

    async def prune_snapshots(self, vmid, keep, kind="lxc"):
        tool = "pct" if kind == "lxc" else "qm"
        async with self._connect() as conn:
            out = await self._run(conn, f"{tool} listsnapshot {int(vmid)}", timeout=60)
            names = sorted(set(SNAP_NAME.findall(out)))
            removed = names[:-keep] if keep > 0 else names
            for n in removed:
                await self._run(conn, f"{tool} delsnapshot {int(vmid)} {n}", timeout=900)
        return removed
