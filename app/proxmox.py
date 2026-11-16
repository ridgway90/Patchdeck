"""Talks to a Proxmox VE host over SSH and runs commands on it and inside its LXCs."""
import asyncio
import json
import os
import re
import secrets
import shlex
import time

import asyncssh

APT_FAMILY = {"debian", "ubuntu", "devuan"}
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


class Host:
    def __init__(self, name, address, port=22, user="root", key_file=None,
                 known_hosts=None, console_url=None, host_console_url=None):
        self.name = name
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

    async def _check_ct(self, conn, ct):
        vmid = int(ct["vmid"])
        entry = {"vmid": vmid, "name": ct.get("name") or str(vmid), "status": ct.get("status"),
                 "os": None, "packages": [], "reboot": False, "error": None, "warning": None,
                 "checked_at": time.time()}
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
            out = await self._run(conn, ct_cmd(vmid, f"{listing_cmd}; echo __REBOOT__; {CT_REBOOT}"))
            listing, _, reboot = out.partition("__REBOOT__")
            entry["packages"] = parse_packages(fam, listing)
            entry["reboot"] = reboot.strip() == "yes"
            if "__UPDATE_FAILED__" in listing:
                entry["warning"] = "Could not refresh package lists"
        except Exception as e:  # one broken container should not fail the whole host
            entry["error"] = str(e)
        return entry

    async def check(self, exclude=()):
        """Full check: host packages plus every LXC on the node."""
        async with self._connect() as conn:
            node = (await self._run(conn, "hostname", timeout=15)).strip()
            host_info = await self._check_self(conn)
            raw = await self._run(conn, f"pvesh get /nodes/{shlex.quote(node)}/lxc --output-format json", timeout=60)
            lxcs = sorted(json.loads(raw), key=lambda c: int(c["vmid"]))
            sem = asyncio.Semaphore(4)

            async def one(ct):
                async with sem:
                    return await self._check_ct(conn, ct)

            cts = await asyncio.gather(*(one(c) for c in lxcs if int(c["vmid"]) not in exclude))
        return {"node": node, "host": host_info, "containers": list(cts),
                "checked_at": time.time(), "error": None}

    async def check_container(self, vmid):
        async with self._connect() as conn:
            node = (await self._run(conn, "hostname", timeout=15)).strip()
            raw = await self._run(conn, f"pvesh get /nodes/{shlex.quote(node)}/lxc --output-format json", timeout=60)
            ct = next((c for c in json.loads(raw) if int(c["vmid"]) == int(vmid)), None)
            if ct is None:
                raise RuntimeError(f"Container {vmid} no longer exists")
            return await self._check_ct(conn, ct)

    async def check_self(self):
        async with self._connect() as conn:
            return await self._check_self(conn)

    # ---------- actions ----------

    async def stream(self, cmd, on_line):
        """Run cmd detached on the host (so a dropped SSH session can't kill apt halfway),
        and stream its output. Returns the command's exit code."""
        log = f"/tmp/patchdeck-{secrets.token_hex(6)}.log"
        inner = f"{cmd}; echo {RC_MARK}$?"
        wrapped = (
            f"setsid nohup sh -c {shlex.quote(inner)} > {log} 2>&1 < /dev/null & PID=$!; "
            f"tail -n +1 -f --pid=$PID {log}; rm -f {log}"
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
            raise RuntimeError("Lost connection before the update finished; check the host")
        return rc

    def upgrade_cmd(self, vmid=None, fam="apt"):
        if vmid is None:
            return APT_UPGRADE
        return ct_cmd(vmid, APT_UPGRADE if fam == "apt" else APK_UPGRADE)

    async def snapshot(self, vmid):
        name = time.strftime("pre-update-%Y%m%d-%H%M%S")
        async with self._connect() as conn:
            await self._run(conn, f"pct snapshot {int(vmid)} {name} --description 'Taken by patchdeck before updating'", timeout=600)
        return name

    async def prune_snapshots(self, vmid, keep):
        async with self._connect() as conn:
            out = await self._run(conn, f"pct listsnapshot {int(vmid)}", timeout=60)
            names = sorted(set(SNAP_NAME.findall(out)))
            removed = names[:-keep] if keep > 0 else names
            for n in removed:
                await self._run(conn, f"pct delsnapshot {int(vmid)} {n}", timeout=600)
        return removed
