"""Helpers for the Settings page: the app's SSH key, known_hosts trust, and key installation."""
import os
import shlex
from pathlib import Path

import asyncssh

SERVER_KEY_FILES = "/etc/ssh/ssh_host_ed25519_key.pub"


def host_pattern(address, port):
    return address if int(port) == 22 else f"[{address}]:{int(port)}"


# ---------- the app's own key

def public_key(key_file):
    pub = Path(f"{key_file}.pub")
    if pub.exists():
        return pub.read_text().strip()
    if Path(key_file).exists():
        return asyncssh.read_private_key(key_file).export_public_key().decode().strip()
    return None


def generate_key(key_file):
    path = Path(key_file)
    if path.exists():
        raise RuntimeError(f"{key_file} already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    key = asyncssh.generate_private_key("ssh-ed25519", comment="patchdeck")
    key.write_private_key(str(path))
    os.chmod(path, 0o600)
    key.write_public_key(f"{path}.pub")
    return key.export_public_key().decode().strip()


def authorize_command(pub):
    """Command to paste into a Proxmox host's shell to authorise the key."""
    q = shlex.quote(pub)
    return (f"mkdir -p /root/.ssh && grep -qxF {q} /root/.ssh/authorized_keys 2>/dev/null "
            f"|| echo {q} >> /root/.ssh/authorized_keys")


# ---------- known_hosts

def _read(known_hosts):
    try:
        return Path(known_hosts).read_text()
    except FileNotFoundError:
        return ""


def is_trusted(known_hosts, address, port):
    text = _read(known_hosts)
    if not text.strip():
        return False
    try:
        return bool(asyncssh.import_known_hosts(text).match(address, address, int(port))[0])
    except Exception:
        return False


async def fetch_host_key(address, port):
    key = await asyncssh.get_server_host_key(address, int(port))
    if key is None:
        raise RuntimeError("Host did not present a key")
    return key


def describe(key):
    return {"fingerprint": key.get_fingerprint("sha256"), "type": key.get_algorithm()}


async def trust(known_hosts, address, port, expected_fingerprint):
    """Fetch the host key again and store it only if it matches what the user confirmed."""
    key = await fetch_host_key(address, port)
    if key.get_fingerprint("sha256") != expected_fingerprint:
        raise RuntimeError("The host's key changed since you viewed it. Fetch it again and re-check.")
    pattern = host_pattern(address, port)
    lines = [l for l in _read(known_hosts).splitlines()
             if l.strip() and l.split()[0] != pattern]
    lines.append(f"{pattern} {key.export_public_key().decode().strip()}")
    path = Path(known_hosts)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)
    return describe(key)


def forget(known_hosts, address, port):
    pattern = host_pattern(address, port)
    text = _read(known_hosts)
    if text:
        lines = [l for l in text.splitlines() if l.strip() and l.split()[0] != pattern]
        Path(known_hosts).write_text("\n".join(lines) + ("\n" if lines else ""))


# ---------- connecting

def explain(e):
    if isinstance(e, asyncssh.HostKeyNotVerifiable):
        return "Host fingerprint is not trusted, or has changed. Use Trust fingerprint."
    if isinstance(e, asyncssh.PermissionDenied):
        return "Permission denied: the key (or password) was not accepted."
    if isinstance(e, (OSError, TimeoutError)):
        return f"Could not connect: {e.strerror or e}" if isinstance(e, OSError) else "Connection timed out"
    return str(e) or e.__class__.__name__


async def install_key(address, port, user, password, known_hosts, pub):
    if not is_trusted(known_hosts, address, port):
        raise RuntimeError("Trust the host fingerprint first.")
    async with asyncssh.connect(address, port=int(port), username=user, password=password,
                                client_keys=None, known_hosts=known_hosts,
                                preferred_auth=["password", "keyboard-interactive"],
                                connect_timeout=15) as conn:
        r = await conn.run(authorize_command(pub), check=False, timeout=30)
        if r.exit_status != 0:
            raise RuntimeError((r.stderr or r.stdout or "").strip() or f"exit code {r.exit_status}")


async def test(address, port, user, key_file, known_hosts):
    if known_hosts and not is_trusted(known_hosts, address, port):
        raise RuntimeError("Host fingerprint is not trusted yet. Do step 1 first.")
    async with asyncssh.connect(address, port=int(port), username=user, client_keys=[key_file],
                                known_hosts=known_hosts, preferred_auth=["publickey"],
                                connect_timeout=15) as conn:
        r = await conn.run(
            "hostname; "
            "if command -v pveversion >/dev/null; then pveversion; "
            "elif command -v proxmox-backup-manager >/dev/null; then proxmox-backup-manager versions | head -n 1; "
            "else . /etc/os-release; echo \"$PRETTY_NAME\"; fi; "
            "if command -v pct >/dev/null; then pct list | tail -n +2 | wc -l; else echo -; fi",
            check=False, timeout=30)
        out = (r.stdout or "").strip().splitlines()
        return {"node": out[0] if out else "?", "version": out[1] if len(out) > 1 else "?",
                "containers": int(out[2]) if len(out) > 2 and out[2].isdigit() else None}
