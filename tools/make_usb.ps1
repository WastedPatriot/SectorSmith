<#
.SYNOPSIS
  Builds a bootable SectorSmith USB stick (WinPE) for cloning onto a PC's own Windows disk.

.DESCRIPTION
  The stick boots into a minimal Windows PE that starts SectorSmith in "waiting" mode. Its screen shows an
  IP address and a pairing code; enter them in SectorSmith on the other PC (Connect a machine -> PC booted
  from USB) and the PC's disks become available as clone targets.

  Requirements (free, one-time): Windows ADK + Windows PE add-on
  https://learn.microsoft.com/windows-hardware/get-started/adk-install

.EXAMPLE
  .\make_usb.ps1 -UsbDrive E:            # ERASES the USB stick in E:
#>
#Requires -RunAsAdministrator
param(
  [Parameter(Mandatory)][ValidatePattern('^[A-Za-z]:$')][string]$UsbDrive,
  [string]$Exe = (Join-Path $PSScriptRoot '..\dist\SectorSmith.exe'),
  [string]$Work = 'C:\SectorSmithPE'
)
$ErrorActionPreference = 'Stop'
$adk = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\Assessment and Deployment Kit'
$env_bat = Join-Path $adk 'Deployment Tools\DandISetEnv.bat'
$ocs = Join-Path $adk 'Windows Preinstallation Environment\amd64\WinPE_OCs'
if (-not (Test-Path $env_bat) -or -not (Test-Path $ocs)) {
  throw 'Windows ADK + WinPE add-on not found. Install both from https://learn.microsoft.com/windows-hardware/get-started/adk-install'
}
if (-not (Test-Path $Exe)) { throw "SectorSmith.exe not found at $Exe (run build.bat first, or download it from Releases)" }

$vol = Get-Volume -DriveLetter $UsbDrive.TrimEnd(':') -ErrorAction Stop
if ($vol.DriveType -ne 'Removable') { Write-Warning "$UsbDrive is not reported as removable ($($vol.DriveType))." }
$answer = Read-Host "Everything on $UsbDrive ($($vol.FileSystemLabel), $([math]::Round($vol.Size/1GB,1)) GB) will be ERASED. Type YES to continue"
if ($answer -ne 'YES') { Write-Host 'Cancelled.'; return }

Write-Host '1/5  Creating WinPE working copy…' -ForegroundColor Cyan
if (Test-Path $Work) { Remove-Item $Work -Recurse -Force }
cmd /c "call `"$env_bat`" && copype amd64 `"$Work`"" | Out-Null
$mount = Join-Path $Work 'mount'
$wim = Join-Path $Work 'media\sources\boot.wim'

Write-Host '2/5  Mounting boot image…' -ForegroundColor Cyan
Dism /Mount-Image /ImageFile:"$wim" /Index:1 /MountDir:"$mount" | Out-Null
try {
  Write-Host '3/5  Adding components, scratch space and SectorSmith…' -ForegroundColor Cyan
  foreach ($pkg in 'WinPE-WMI', 'WinPE-Scripting') {
    Dism /Add-Package /Image:"$mount" /PackagePath:"$ocs\$pkg.cab" | Out-Null
  }
  Dism /Set-ScratchSpace:512 /Image:"$mount" | Out-Null   # room to unpack the single-file exe
  Copy-Item $Exe (Join-Path $mount 'Windows\System32\SectorSmith.exe') -Force
  @'
@echo off
wpeinit
wpeutil disablefirewall
echo Starting SectorSmith (waiting for a controller)...
X:\Windows\System32\SectorSmith.exe --agent --listen --no-elevate
cmd /k
'@ | Set-Content -Encoding ASCII (Join-Path $mount 'Windows\System32\startnet.cmd')
}
finally {
  Write-Host '4/5  Saving boot image…' -ForegroundColor Cyan
  Dism /Unmount-Image /MountDir:"$mount" /Commit | Out-Null
}
Write-Host "5/5  Writing to $UsbDrive…" -ForegroundColor Cyan
cmd /c "call `"$env_bat`" && MakeWinPEMedia /UFD /F `"$Work`" $UsbDrive"
Write-Host "Done. Boot the target PC from $UsbDrive (UEFI), then use Connect a machine -> PC booted from USB." -ForegroundColor Green
