"""Windows Update for Windows VMs, driven through the QEMU guest agent.

Everything runs inside the VM with PowerShell (5.1, built into Windows Server 2016+ / Windows 10+)
and the built-in Windows Update Agent COM API, so nothing extra has to be installed.

- Checks run the search directly (qemu-ga runs as LocalSystem).
- Installs run as a one-off scheduled task as SYSTEM: Windows Update refuses to download or
  install from some remote contexts, and the task keeps going if the guest agent restarts.
  The task writes a log that Patchdeck polls.
"""
import base64
import json
import secrets

RC_MARK = "__PATCHDECK_RC__"
TEMP = r"C:\Windows\Temp"


def criteria(drivers):
    # BrowseOnly=0 skips optional/preview updates, as Windows' own automatic updates do.
    base = "IsInstalled=0 and IsHidden=0 and BrowseOnly=0"
    return base if drivers else base + " and Type='Software'"


def encode(script):
    """powershell.exe -EncodedCommand wants base64 of UTF-16LE."""
    return base64.b64encode(script.encode("utf-16-le")).decode()


def ps_command(vmid, script, timeout):
    return (f"qm guest exec {int(vmid)} --timeout {int(timeout)} -- powershell.exe -NoProfile -NonInteractive "
            f"-ExecutionPolicy Bypass -EncodedCommand {encode(script)}")


# Lines shared by every script: UTF-8 output, no progress bars, stop on errors.
PRELUDE = r"""$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }   # fails without a console on some builds
$ErrorActionPreference = 'Stop'
"""

CHECK = PRELUDE + r"""# PATCHDECK:CHECK
$session = New-Object -ComObject Microsoft.Update.Session
$session.ClientApplicationID = 'Patchdeck'
$result = $session.CreateUpdateSearcher().Search("__CRITERIA__")
$list = @()
foreach ($u in $result.Updates) {
    $cats = @($u.Categories | ForEach-Object { $_.Name })
    if ($cats -contains 'Upgrades') { continue }   # feature upgrades are left to Windows
    $list += [pscustomobject]@{
        title    = $u.Title
        kb       = (@($u.KBArticleIDs | ForEach-Object { "KB$_" }) -join ', ')
        size     = [int64]$u.MaxDownloadSize
        category = ($cats -join ', ')
        severity = [string]$u.MsrcSeverity
    }
}
$keys = @(
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending',
    'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired'
)
$reboot = (New-Object -ComObject Microsoft.Update.SystemInfo).RebootRequired
foreach ($k in $keys) { if (Test-Path $k) { $reboot = $true } }
$os = (Get-CimInstance Win32_OperatingSystem).Caption
[pscustomobject]@{ updates = $list; reboot = [bool]$reboot; os = $os } | ConvertTo-Json -Depth 4 -Compress
"""

# Runs as SYSTEM from a scheduled task. __LOG__ and __CRITERIA__ are filled in by Python.
UPDATE = r"""$ProgressPreference = 'SilentlyContinue'
$log = '__LOG__'
function Log($m) { [IO.File]::AppendAllText($log, "$m`r`n") }
$codes = @{ 0 = 'Not started'; 1 = 'In progress'; 2 = 'Installed'; 3 = 'Installed with errors'; 4 = 'Failed'; 5 = 'Aborted' }
try {
    $session = New-Object -ComObject Microsoft.Update.Session
    $session.ClientApplicationID = 'Patchdeck'
    Log 'Searching for updates...'
    $result = $session.CreateUpdateSearcher().Search("__CRITERIA__")
    $todo = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($u in $result.Updates) {
        $cats = @($u.Categories | ForEach-Object { $_.Name })
        if ($cats -contains 'Upgrades') { continue }
        if (-not $u.EulaAccepted) { $u.AcceptEula() }
        [void]$todo.Add($u)
        Log "  $($u.Title)"
    }
    if ($todo.Count -eq 0) { Log 'Nothing to install.'; Log '__RC__0'; exit 0 }
    Log "Downloading $($todo.Count) update(s)..."
    $dl = $session.CreateUpdateDownloader()
    $dl.Updates = $todo
    $dlResult = $dl.Download()
    Log "Download: $($codes[[int]$dlResult.ResultCode])"
    $ready = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($u in $todo) { if ($u.IsDownloaded) { [void]$ready.Add($u) } }
    if ($ready.Count -eq 0) { Log 'Nothing could be downloaded.'; Log '__RC__1'; exit 1 }
    Log "Installing $($ready.Count) update(s)... (this can take a while)"
    $inst = $session.CreateUpdateInstaller()
    $inst.Updates = $ready
    $res = $inst.Install()
    $failed = 0
    for ($i = 0; $i -lt $ready.Count; $i++) {
        $code = [int]$res.GetUpdateResult($i).ResultCode
        if ($code -ne 2) { $failed++ }
        Log "  $($codes[$code]): $($ready.Item($i).Title)"
    }
    Log "Reboot required: $($res.RebootRequired)"
    if ($failed -gt 0) { Log "$failed update(s) did not install."; Log '__RC__2' } else { Log '__RC__0' }
} catch {
    Log "ERROR: $($_.Exception.Message)"
    Log '__RC__1'
}
""".replace("__RC__", RC_MARK)


def start_script(tag, drivers):
    """Write the update script into the VM and start it as a SYSTEM scheduled task."""
    ps1, log = f"{TEMP}\\patchdeck-{tag}.ps1", f"{TEMP}\\patchdeck-{tag}.log"
    body = UPDATE.replace("__LOG__", log).replace("__CRITERIA__", criteria(drivers))
    b64 = base64.b64encode(body.encode("utf-8")).decode()
    return PRELUDE + f"""# PATCHDECK:START
[IO.File]::WriteAllBytes('{ps1}', [Convert]::FromBase64String('{b64}'))
[IO.File]::WriteAllText('{log}', '')
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{ps1}"'
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 6)
Register-ScheduledTask -TaskName 'Patchdeck-{tag}' -Action $action -Settings $settings -User 'NT AUTHORITY\\SYSTEM' -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName 'Patchdeck-{tag}'
'started'
""", log


def poll_script(log, offset):
    return PRELUDE + f"""# PATCHDECK:POLL
$bytes = [IO.File]::ReadAllBytes('{log}')
"__LEN__$($bytes.Length)"
if ($bytes.Length -gt {int(offset)}) {{ [Text.Encoding]::UTF8.GetString($bytes, {int(offset)}, $bytes.Length - {int(offset)}) }}
"""


def cleanup_script(tag):
    return PRELUDE + f"""# PATCHDECK:CLEANUP
Unregister-ScheduledTask -TaskName 'Patchdeck-{tag}' -Confirm:$false -ErrorAction SilentlyContinue
Remove-Item '{TEMP}\\patchdeck-{tag}.ps1', '{TEMP}\\patchdeck-{tag}.log' -ErrorAction SilentlyContinue
"""


def new_tag():
    return secrets.token_hex(6)


def parse_check(out):
    data = json.loads(out.strip().lstrip("\ufeff") or "{}")
    ups = data.get("updates") or []
    if isinstance(ups, dict):  # ConvertTo-Json turns a one-item array into an object
        ups = [ups]
    pkgs = [{"name": u.get("title") or "Update", "kb": u.get("kb") or "", "size": int(u.get("size") or 0),
             "category": u.get("category") or "", "severity": u.get("severity") or "",
             "from": "", "to": u.get("kb") or u.get("title") or ""} for u in ups]
    return pkgs, bool(data.get("reboot")), data.get("os")
