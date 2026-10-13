"""Built-in maintenance tasks: common MSP upkeep as a Test (exit 0 when the PC is fine), Get (prints the current
state) and Set (makes it so) PowerShell trio with typed parameters. Add one to the library from Manage, Tasks.

Each script gets its parameters as variables set at the top (see taskparams.render), plus $SectorSmithClient and
$SectorSmithComputer. Set exits 3010 when the change needs a restart."""
from __future__ import annotations

from . import core

# small helpers shared by the registry-based tasks
_REG = r"""function Get-Reg($Path, $Name) {
    try { (Get-ItemProperty -LiteralPath $Path -Name $Name -ErrorAction Stop).$Name } catch { $null }
}
function Set-Reg($Path, $Name, $Value, $Type = 'DWord') {
    if (-not (Test-Path -LiteralPath $Path)) { New-Item -Path $Path -Force | Out-Null }
    New-ItemProperty -LiteralPath $Path -Name $Name -Value $Value -PropertyType $Type -Force | Out-Null
}
"""

_RENAME = r"""function Get-WantedName {
    $serial = "$((Get-CimInstance Win32_BIOS).SerialNumber)".Trim()
    $n = $Pattern.Replace('{serial}', $serial).Replace('{client}', "$SectorSmithClient")
    $n = ($n -replace '[^A-Za-z0-9-]', '').Trim('-')
    if ($n.Length -gt 15) { $n = $n.Substring(0, 15) }
    $n.ToUpper()
}
function Get-PendingName {
    (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\ComputerName\ComputerName').ComputerName
}
"""

_POWER = r"""$plans = @{ 'Balanced' = '381b4222-f694-41f0-9685-ff5bb260df2e';
           'High performance' = '8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c';
           'Power saver' = 'a1841308-3541-4fab-bc81-f71556f20b4a' }
function Get-ActivePlan { ([regex]::Match((powercfg /getactivescheme | Out-String), '[0-9a-f-]{36}')).Value }
function Get-SleepMinutes {
    $q = powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE | Out-String
    $m = [regex]::Match($q, 'AC Power Setting Index:\s*0x([0-9a-fA-F]+)')
    if ($m.Success) { [int]([Convert]::ToInt32($m.Groups[1].Value, 16) / 60) } else { -1 }
}
"""

_WU = _REG + r"""$au = 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate'
$ux = 'HKLM:\SOFTWARE\Microsoft\WindowsUpdate\UX\Settings'
$want = @(
    @($ux, 'ActiveHoursStart', [int]$ActiveHoursStart),
    @($ux, 'ActiveHoursEnd', [int]$ActiveHoursEnd),
    @($au, 'DeferQualityUpdates', [int]([int]$DeferQualityDays -gt 0)),
    @($au, 'DeferQualityUpdatesPeriodInDays', [int]$DeferQualityDays),
    @($au, 'DeferFeatureUpdates', [int]([int]$DeferFeatureDays -gt 0)),
    @($au, 'DeferFeatureUpdatesPeriodInDays', [int]$DeferFeatureDays),
    @("$au\AU", 'NoAutoRebootWithLoggedOnUsers', [int][bool]$NoRestartWhileSignedIn)
)
"""

_APPX = r"""$patterns = @($Apps -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
function Get-Bloat {
    $found = @()
    foreach ($p in $patterns) {
        $found += Get-AppxPackage -AllUsers -Name $p -ErrorAction SilentlyContinue | ForEach-Object { $_.Name }
        $found += Get-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue |
                  Where-Object { $_.DisplayName -like $p } | ForEach-Object { $_.DisplayName }
    }
    @($found | Sort-Object -Unique)
}
"""

_OEM = _REG + r"""$key = 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\OEMInformation'
$want = [ordered]@{ Manufacturer = $Manufacturer; SupportPhone = $Phone; SupportHours = $Hours; SupportURL = $Url }
"""

LIBRARY = [
    {"library_id": "rename-computer", "name": "Computer name follows a pattern", "category": "Setup",
     "description": "Renames the PC from a pattern such as ACME-{serial}. {serial} is the BIOS serial number, "
                    "{client} the client's name. Domain-joined PCs need the rename done with domain rights.",
     "parameters": [{"name": "Pattern", "type": "text", "default": "{client}-{serial}", "required": True,
                     "help": "Up to 15 letters, digits and dashes after the tokens are filled in."}],
     "test": _RENAME + "if ((Get-PendingName) -eq (Get-WantedName)) { exit 0 }\n"
                       "Write-Output \"Name is $(Get-PendingName), wanted $(Get-WantedName)\"\nexit 1\n",
     "get": _RENAME + "Get-PendingName\n",
     "set": _RENAME + "$n = Get-WantedName\nif (-not $n) { Write-Error 'The pattern gave an empty name'; exit 1 }\n"
                      "Rename-Computer -NewName $n -Force -ErrorAction Stop\nWrite-Output \"Renamed to $n\"\n"
                      "exit 3010\n"},
    {"library_id": "time-zone", "name": "Time zone", "category": "Setup",
     "description": "Sets the Windows time zone, for example GMT Standard Time (tzutil /l lists them).",
     "parameters": [{"name": "TimeZone", "type": "text", "default": "GMT Standard Time", "required": True}],
     "test": "if ((Get-TimeZone).Id -eq $TimeZone) { exit 0 }\nexit 1\n",
     "get": "(Get-TimeZone).Id\n",
     "set": "Set-TimeZone -Id $TimeZone -ErrorAction Stop\n"},
    {"library_id": "power-plan", "name": "Power plan and sleep", "category": "Setup",
     "description": "Picks the active power plan and how long the PC waits on mains power before it sleeps "
                    "(0 is never).",
     "parameters": [{"name": "Plan", "type": "choice", "choices": ["Balanced", "High performance", "Power saver"],
                     "default": "Balanced"},
                    {"name": "SleepAfterMinutes", "label": "Sleep after (minutes)", "type": "number", "default": 0}],
     "test": _POWER + "if ((Get-ActivePlan) -eq $plans[$Plan] -and (Get-SleepMinutes) -eq [int]$SleepAfterMinutes) "
                      "{ exit 0 }\nexit 1\n",
     "get": _POWER + "Write-Output \"Plan $(Get-ActivePlan), sleep after $(Get-SleepMinutes) min\"\n",
     "set": _POWER + "powercfg /setactive $plans[$Plan]\nif ($LASTEXITCODE) { exit $LASTEXITCODE }\n"
                     "powercfg /change standby-timeout-ac ([int]$SleepAfterMinutes)\nexit $LASTEXITCODE\n"},
    {"library_id": "bitlocker", "name": "BitLocker on the system drive", "category": "Security",
     "description": "Check only reports BitLocker on the Windows drive. Check and fix turns it on with the TPM and "
                    "a recovery password, backed up where you choose. Recovery keys are never printed.",
     "parameters": [{"name": "EncryptionMethod", "type": "choice", "choices": ["XtsAes128", "XtsAes256"],
                     "default": "XtsAes128"},
                    {"name": "BackupTo", "label": "Back up the key to", "type": "choice",
                     "choices": ["Active Directory", "Entra ID", "Nowhere"], "default": "Entra ID"}],
     "test": "$v = Get-BitLockerVolume -MountPoint $env:SystemDrive -ErrorAction Stop\n"
             "if ($v.ProtectionStatus -eq 'On') { exit 0 }\n"
             "Write-Output \"BitLocker is $($v.VolumeStatus), protection $($v.ProtectionStatus)\"\nexit 1\n",
     "get": "$v = Get-BitLockerVolume -MountPoint $env:SystemDrive -ErrorAction Stop\n"
            "Write-Output \"$($v.VolumeStatus), protection $($v.ProtectionStatus), $($v.EncryptionMethod), "
            "$($v.EncryptionPercentage)%\"\n",
     "set": "$ErrorActionPreference = 'Stop'\n"
            "$tpm = Get-Tpm\nif (-not $tpm.TpmReady) { Write-Error 'The TPM is missing or not ready'; exit 1 }\n"
            "$v = Get-BitLockerVolume -MountPoint $env:SystemDrive\n"
            "$rp = @($v.KeyProtector | Where-Object KeyProtectorType -eq 'RecoveryPassword')\n"
            "if (-not $rp) { Add-BitLockerKeyProtector -MountPoint $env:SystemDrive -RecoveryPasswordProtector "
            "| Out-Null }\n"
            "$rp = @((Get-BitLockerVolume -MountPoint $env:SystemDrive).KeyProtector |\n"
            "        Where-Object KeyProtectorType -eq 'RecoveryPassword')[0]\n"
            "if ($BackupTo -eq 'Active Directory') { Backup-BitLockerKeyProtector -MountPoint $env:SystemDrive "
            "-KeyProtectorId $rp.KeyProtectorId | Out-Null }\n"
            "if ($BackupTo -eq 'Entra ID') { BackupToAAD-BitLockerKeyProtector -MountPoint $env:SystemDrive "
            "-KeyProtectorId $rp.KeyProtectorId | Out-Null }\n"
            "if ($v.VolumeStatus -eq 'FullyDecrypted') {\n"
            "    Enable-BitLocker -MountPoint $env:SystemDrive -EncryptionMethod $EncryptionMethod -UsedSpaceOnly "
            "-TpmProtector -SkipHardwareTest | Out-Null\n"
            "} else { Resume-BitLocker -MountPoint $env:SystemDrive | Out-Null }\n"
            "Write-Output 'BitLocker is on. The recovery key was not printed.'\n"},
    {"library_id": "local-admin", "name": "Local admin account", "category": "Security",
     "description": "Keeps a local account (the built-in Administrator by default) enabled or disabled, and can "
                    "set its password from an encrypted secret.",
     "parameters": [{"name": "Account", "type": "text", "default": "Administrator", "required": True},
                    {"name": "State", "type": "choice", "choices": ["Disabled", "Enabled"], "default": "Disabled"},
                    {"name": "Password", "type": "secret",
                     "help": "Only used when enabling. Leave empty to keep the current password."}],
     "test": "$u = Get-LocalUser -Name $Account -ErrorAction SilentlyContinue\n"
             "if (-not $u) { if ($State -eq 'Disabled') { exit 0 }; Write-Output \"No account $Account\"; exit 1 }\n"
             "if ($u.Enabled -eq ($State -eq 'Enabled')) { exit 0 }\nexit 1\n",
     "get": "$u = Get-LocalUser -Name $Account -ErrorAction SilentlyContinue\n"
            "if ($u) { if ($u.Enabled) { 'Enabled' } else { 'Disabled' } } else { 'Missing' }\n",
     "set": "$ErrorActionPreference = 'Stop'\n$u = Get-LocalUser -Name $Account -ErrorAction SilentlyContinue\n"
            "if ($State -eq 'Disabled') { if ($u) { Disable-LocalUser -Name $Account }; exit 0 }\n"
            "$pw = $null\nif ($Password) { $pw = ConvertTo-SecureString $Password -AsPlainText -Force }\n"
            "if (-not $u) {\n    if (-not $pw) { Write-Error 'Give a password to create the account'; exit 1 }\n"
            "    New-LocalUser -Name $Account -Password $pw -PasswordNeverExpires | Out-Null\n"
            "    Add-LocalGroupMember -SID 'S-1-5-32-544' -Member $Account\n"
            "} elseif ($pw) { Set-LocalUser -Name $Account -Password $pw }\n"
            "Enable-LocalUser -Name $Account\n"},
    {"library_id": "windows-update", "name": "Windows Update settings", "category": "Updates",
     "description": "Active hours, how many days quality and feature updates wait, and no automatic restart while "
                    "someone is signed in.",
     "parameters": [{"name": "ActiveHoursStart", "label": "Active hours start", "type": "number", "default": 8},
                    {"name": "ActiveHoursEnd", "label": "Active hours end", "type": "number", "default": 18},
                    {"name": "DeferQualityDays", "label": "Delay quality updates (days)", "type": "number",
                     "default": 0},
                    {"name": "DeferFeatureDays", "label": "Delay feature updates (days)", "type": "number",
                     "default": 0},
                    {"name": "NoRestartWhileSignedIn", "label": "No restart while signed in", "type": "bool",
                     "default": True}],
     "test": _WU + "foreach ($w in $want) {\n    if ((Get-Reg $w[0] $w[1]) -ne $w[2]) { "
                   "Write-Output \"$($w[1]) is $(Get-Reg $w[0] $w[1]), wanted $($w[2])\"; exit 1 }\n}\nexit 0\n",
     "get": _WU + "foreach ($w in $want) { Write-Output \"$($w[1]) = $(Get-Reg $w[0] $w[1])\" }\n",
     "set": _WU + "foreach ($w in $want) { Set-Reg $w[0] $w[1] $w[2] }\n"},
    {"library_id": "remove-bloatware", "name": "Remove common bloatware apps", "category": "Setup",
     "description": "Removes Store apps that come with Windows for every user and stops them coming back for new "
                    "users. Names can use * as a wildcard.",
     "parameters": [{"name": "Apps", "type": "text", "required": True,
                     "default": "Microsoft.BingNews, Microsoft.BingWeather, Microsoft.GetHelp, "
                                "Microsoft.MicrosoftSolitaireCollection, Microsoft.People, "
                                "Microsoft.WindowsFeedbackHub, Microsoft.XboxApp, Microsoft.ZuneMusic, "
                                "Microsoft.ZuneVideo, Clipchamp.Clipchamp, Microsoft.549981C3F5F10"}],
     "test": _APPX + "$b = Get-Bloat\nif (-not $b) { exit 0 }\nWrite-Output ($b -join ', ')\nexit 1\n",
     "get": _APPX + "$b = Get-Bloat\nif ($b) { $b -join ', ' } else { 'None found' }\n",
     "set": _APPX + "foreach ($p in $patterns) {\n"
                    "    Get-AppxPackage -AllUsers -Name $p -ErrorAction SilentlyContinue |\n"
                    "        Remove-AppxPackage -AllUsers -ErrorAction SilentlyContinue\n"
                    "    Get-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue |\n"
                    "        Where-Object { $_.DisplayName -like $p } |\n"
                    "        Remove-AppxProvisionedPackage -Online -ErrorAction SilentlyContinue | Out-Null\n}\n"},
    {"library_id": "defender", "name": "Defender real-time and cloud protection", "category": "Security",
     "description": "Keeps Microsoft Defender's real-time and cloud-delivered protection on (or off where another "
                    "antivirus takes over). Tamper Protection can block changes; set it in your security portal.",
     "parameters": [{"name": "RealTime", "label": "Real-time protection", "type": "bool", "default": True},
                    {"name": "CloudProtection", "label": "Cloud protection", "type": "bool", "default": True}],
     "test": "$p = Get-MpPreference -ErrorAction Stop\n"
             "$rt = -not $p.DisableRealtimeMonitoring\n$cloud = $p.MAPSReporting -ne 0\n"
             "if ($rt -eq [bool]$RealTime -and $cloud -eq [bool]$CloudProtection) { exit 0 }\n"
             "Write-Output \"Real-time $rt, cloud $cloud\"\nexit 1\n",
     "get": "$p = Get-MpPreference -ErrorAction Stop\n"
            "Write-Output \"Real-time $(-not $p.DisableRealtimeMonitoring), cloud $($p.MAPSReporting -ne 0)\"\n",
     "set": "$maps = 0\nif ($CloudProtection) { $maps = 2 }\n"
            "Set-MpPreference -DisableRealtimeMonitoring (-not [bool]$RealTime) -MAPSReporting $maps "
            "-ErrorAction Stop\n"},
    {"library_id": "fast-startup", "name": "Fast startup off", "category": "Setup",
     "description": "Fast startup makes a shut down PC resume a saved kernel, so updates and drivers don't fully "
                    "apply until a restart. Turning it off makes Shut down a real shut down.",
     "parameters": [{"name": "FastStartup", "label": "Fast startup on", "type": "bool", "default": False}],
     "test": _REG + "$k = 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power'\n"
                    "if ((Get-Reg $k 'HiberbootEnabled') -eq [int][bool]$FastStartup) { exit 0 }\nexit 1\n",
     "get": _REG + "$k = 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power'\n"
                   "if ((Get-Reg $k 'HiberbootEnabled') -eq 1) { 'On' } else { 'Off' }\n",
     "set": _REG + "$k = 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Power'\n"
                   "Set-Reg $k 'HiberbootEnabled' ([int][bool]$FastStartup)\n"},
    {"library_id": "oem-info", "name": "Support details in Settings, About", "category": "Branding",
     "description": "Your company name, phone, hours and website in Settings, System, About, so users know who "
                    "to call.",
     "parameters": [{"name": "Manufacturer", "label": "Company", "type": "text", "required": True},
                    {"name": "Phone", "type": "text"}, {"name": "Hours", "type": "text",
                                                        "default": "Monday to Friday, 9am to 5pm"},
                    {"name": "Url", "label": "Website", "type": "text"}],
     "test": _OEM + "foreach ($n in $want.Keys) { if (\"$(Get-Reg $key $n)\" -ne \"$($want[$n])\") { exit 1 } }\n"
                    "exit 0\n",
     "get": _OEM + "foreach ($n in $want.Keys) { Write-Output \"$n = $(Get-Reg $key $n)\" }\n",
     "set": _OEM + "foreach ($n in $want.Keys) { Set-Reg $key $n \"$($want[$n])\" 'String' }\n"},
]


def get(library_id: str) -> dict | None:
    return next((t for t in LIBRARY if t["library_id"] == library_id), None)


def to_task(entry: dict) -> core.Task:
    from .taskparams import tidy_parameters
    return core.Task(name=entry["name"], test=entry["test"], set=entry["set"], get=entry.get("get", ""),
                     description=entry["description"], language="powershell",
                     parameters=tidy_parameters(entry["parameters"]), category=entry.get("category", ""),
                     library_id=entry["library_id"])


def add(store: core.Store, library_id: str) -> core.Task:
    """Adds a built-in task to the library, or returns the copy that's already there."""
    have = next((t for t in store.tasks if t.library_id == library_id), None)
    if have is not None:
        return have
    entry = get(library_id)
    if entry is None:
        raise ValueError(f"No built-in task called {library_id}.")
    return store.upsert("tasks", to_task(entry))
