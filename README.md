# Patchdeck

![Patchdeck](screenshot.png)

A small, self-hosted update dashboard for Proxmox VE hosts and their LXC containers.

- Lists every LXC on each host, grouped by host, with pending update counts and package lists
- **Update** buttons per container, per host, and **Update all containers**
- Optional Proxmox **snapshot before update**, with old pre-update snapshots pruned automatically
- **Live log** of each update in the page
- **Console** buttons that open the Proxmox console (or Nexterm, or any URL you configure)
- Scheduled checks with a **Discord** summary, plus a message when updates finish
- **Containers and VMs**: LXCs through `pct exec`, VMs through the QEMU guest agent
  (install `qemu-guest-agent` in the VM and enable *QEMU Guest Agent* in its Options)
- **Update dialog**: update now or schedule for a maintenance window, snapshot first,
  and reboot afterwards if the update needs it
- **Maintenance windows and schedules**: several named windows; give any container, VM or
  host a window, optionally updating automatically in it; one Discord summary per window
- **Docker Compose stacks** inside containers, VMs and Debian/Ubuntu servers: Patchdeck asks each
  image's registry (Docker Hub, GHCR, lscr.io, Quay…) whether a newer version exists, without
  pulling anything, and updates a stack with `docker compose pull && up -d`, now or in a window
- **Proxmox Backup Server** and plain Debian/Ubuntu servers can be added as hosts too
- Debian, Ubuntu, Devuan and Alpine guests, and **Windows VMs** (Windows Update through the guest agent)

It connects only to your Proxmox hosts over SSH and runs commands inside containers with
`pct exec`, so containers need no SSH server, key or agent. New containers are picked up automatically.

## Install (Debian LXC)

Run as root in a fresh Debian container (unprivileged is fine; no special features needed):

```bash
apt update && apt install -y git python3-venv
```

```bash
git clone https://github.com/ridgway90/Patchdeck /opt/patchdeck && cd /opt/patchdeck && python3 -m venv venv && venv/bin/pip install -r requirements.txt
```

Create a minimal config with a login password:

```bash
mkdir -p data && chmod 700 data && printf 'auth:\n  username: admin\n  password: CHANGE-ME\n' > data/config.yaml
```

```bash
cp patchdeck.service /etc/systemd/system/ && systemctl daemon-reload && systemctl enable --now patchdeck
```

Set the container's timezone so scheduled checks run at your local time (e.g. `timedatectl set-timezone Europe/London`).

Or with Docker: create `data/config.yaml` the same way, then `docker compose up -d --build`.

## Setup (in the web UI)

Open `http://<ip>:8080`, sign in, and go to **Settings**:

1. **Generate SSH key**: Patchdeck creates its own key in `data/`.
2. **Add a host**: name, IP, SSH port and type (Proxmox VE, Proxmox Backup Server, or another
   Debian/Ubuntu server) for each machine.
3. **Trust the fingerprint**: Patchdeck shows the host's SSH fingerprint and the command to check
   it on the host (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` in the Proxmox shell).
   Confirm they match and click **It matches, trust it**.
4. **Install key**: enter the host's root password once (it's used to add the key and is not stored),
   or copy the command shown and paste it into the host's shell yourself.
5. **Test connection**, then go back to the Dashboard and click **Check now**.

Also in Settings: maintenance windows and per-guest maintenance schedules, check schedule (days and times), Discord webhook and test message, snapshot
defaults, the dashboard address used in Discord links, and container IDs to hide.

Settings are saved to `data/config.yaml` (the first save keeps a copy as `config.yaml.bak`).
You can still edit the file by hand; see `config.example.yaml` for every option, then
`systemctl restart patchdeck`.

## Updating Patchdeck

```bash
cd /opt/patchdeck && git pull && venv/bin/pip install -r requirements.txt && systemctl restart patchdeck
```

## Windows VMs

Windows VMs (Windows Server 2016+ / Windows 10+) are checked and updated with Windows' own
Windows Update service, driven through the QEMU guest agent; nothing else is installed in the VM.

- **Needs** the QEMU guest agent from the VirtIO guest tools (`virtio-win-guest-tools.exe` on the
  virtio-win ISO) and *QEMU Guest Agent* enabled in the VM's Options in Proxmox.
- **Check** lists pending updates (KB, title, size) and whether Windows wants a reboot. Optional
  and preview updates and feature upgrades are left out, like Windows' own automatic updates.
  Driver updates are left out unless *Include driver updates for Windows VMs* is ticked in
  Settings → General.
- **Update** starts a one-off scheduled task inside the VM, running as SYSTEM, which searches,
  downloads and installs the updates and writes a log that Patchdeck shows every few seconds.
  The task and its files are removed afterwards.
- **Reboot afterwards** uses `qm reboot` and waits up to 20 minutes for Windows to finish
  "working on updates" and the guest agent to come back.
- Windows keeps its own automatic update schedule; Patchdeck checks and updates alongside it.
  Snapshots, maintenance windows and auto-update work as for Linux guests.

## Docker stacks

Any container, VM or Debian/Ubuntu server with Docker and the Compose plugin has its stacks listed
under it (anything that shows up in `docker compose ls`). Each check compares the digest of every
local image with the digest of the same tag in its registry:

- **New version**: the tag now points at a different image. **Update…** runs `docker compose pull`
  and `docker compose up -d` in the stack's folder, then `docker image prune -f` (can be turned off
  in Settings → General). Snapshots, maintenance windows and auto-update work as for OS updates.
- **Local build**: images built from a Dockerfile can't be compared and are skipped.
- **Can't check**: private images, pinned digests, or a registry rate limit. Lookups use `HEAD`
  requests, which Docker Hub allows about 100 an hour per IP for anonymous clients; a homelab
  checked a few times a day stays well below that.

**Registry logins** (Settings → Docker): add a Docker Hub username and a read-only personal access
token to raise Docker Hub's limit, or a login for ghcr.io or a private registry so its private
images can be checked. **Test login** confirms it works and, for Docker Hub, shows how many lookups
are left. Tokens are stored in `config.yaml` and never sent back to the browser. Docker Hub can also
be set with the `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` environment variables.

Patchdeck needs outbound HTTPS to the registries for this. Turn it off under Settings → General.

## Security notes

- The SSH key gives root on your Proxmox hosts. Keep `data/` private and only expose Patchdeck
  on your LAN or Tailscale, not the internet.
- The root password typed into **Install key** is sent to Patchdeck and then to the host over SSH;
  it is never written to disk.
- Login uses HTTP Basic auth, so use HTTPS (e.g. `tailscale serve`) if you reach it from outside your LAN.

## How updates run

- Containers: `apt-get dist-upgrade` (keeping existing config files) or `apk upgrade`.
- Hosts: `apt-get dist-upgrade`, as Proxmox recommends. Patchdeck doesn't reboot anything; it shows
  **Reboot needed** when a new kernel is installed or a package asks for a reboot.
- Updates run detached on the host, so a dropped connection won't kill `apt` halfway.
- Updates on the same host run one at a time; different hosts run in parallel.
- If a snapshot fails (e.g. the container's storage doesn't support snapshots), the update is
  aborted unless `snapshots.on_failure: continue` is set. Untick **Snapshot first** to skip it.
