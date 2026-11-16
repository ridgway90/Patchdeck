"""Talks to a Proxmox VE host over SSH and runs commands on it and inside its LXCs."""
import asyncio
import json
import os
import re
import secrets
import shlex
import time

import asyncssh

APT_FAMILY = {"debian", "ubuntu", "devuan", "raspbian", "linuxmint", "pop", "kali"}
APK_FAMILY = {"alpine"}

# Lists pending updates. Prints __UPDATE_FAILED__ if the package index could not be refreshed.
APT_LIST = (
    "apt-get update -qq >/dev/null 2>&1 || echo __UPDATE_FAILED__; "
    "apt list --upgradable 2>/dev/null"
)
APK_LIST = (
    "apk update -q >/dev/null 2>&1 || echo __UPDATE_FAILED__; "
    "apk version -l '<' 2>/dev/null"
)
APT_UPGRADE = (
    "export DEBIAN_FRONTEND=noninteractive; "
    "apt-get update && "
    "apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold dist-upgrade && "
    "apt-get -y autoremove"
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
APK_LINE = re.compile(r"^(\S+?)-(\d\S*)\s+<\s+(\S+)")
SNAP_NAME = re.compile(r"(pre-update-\d{8}-\d{6})")
RC_MARK = "__PATCHDECK_RC__"


def family(ostype):
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
        r = await conn.run(cmd, check=False, timeout=timeout)
        if check and r.exit_status != 0:
            err = (r.stderr or r.stdout or "").strip().splitlines()
            raise RuntimeError(f"`{cmd.split(' --')[0][:60]}` failed: {err[-1] if err else r.exit_status}")
        return r.stdout or ""

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
                "error": None, "warning": None, "checked_at": time.time()}

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
                entry["error"] = f"Unsupported OS type: {entry['os']}"
                return entry
            listing_cmd = APT_LIST if fam == "apt" else APK_LIST
            self._fill(entry, fam, await self._run(conn, ct_cmd(vmid, f"{listing_cmd}; echo __REBOOT__; {CT_REBOOT}")))
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
            fam = family(entry["os"])
            if not fam:
                entry["error"] = f"Unsupported OS: {info.get('pretty-name') or entry['os']}"
                return entry
            listing_cmd = APT_LIST if fam == "apt" else APK_LIST
            _, out, _ = await self._guest_exec(conn, vmid, f"{listing_cmd}; echo __REBOOT__; {CT_REBOOT}")
            self._fill(entry, fam, out)
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
                return {"node": node, "host": await self._check_self(conn), "containers": [],
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
        """Run cmd detached on the host (so a dropped SSH session can't kill apt halfway),
        and stream its output. Returns the command's exit code."""
        log = f"/tmp/patchdeck-{secrets.token_hex(6)}.log"
        inner = f"( {cmd} ); echo {RC_MARK}$?"  # subshell: the marker is written even if cmd exits
        # Create the log before starting the job: the background shell opens it asynchronously,
        # and tail would otherwise race it and exit with "No such file". Keep the log if the
        # job never reported an exit code, so it can be inspected on the host.
        wrapped = (
            f": > {log}; "
            f"setsid nohup sh -c {shlex.quote(inner)} >> {log} 2>&1 < /dev/null & PID=$!; "
            f"tail -n +1 -F --pid=$PID {log} 2>/dev/null; "
            f"grep -q {RC_MARK} {log} && rm -f {log}"
        )
        rc = None
        async with self._connect() as conn:
            async with conn.create_process(wrapped, stderr=asyncssh.STDOUT,
                                           encoding="utf-8", errors="replace") as proc:
                async for line in proc.stdout:
                    line = line.rstrip("\n")
                    if line.startswith(RC_MARK):
                        rc = int(line[len(RC_MARK):] or 1)
                        continue
                    on_line(line)
                await proc.wait()
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
            await self._run(conn, f"{tool} reboot {int(vmid)} --timeout 300", timeout=420)
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
        on_line("Still not back after 5 minutes; check it in Proxmox.")
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
