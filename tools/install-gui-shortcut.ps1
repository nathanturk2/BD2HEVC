[CmdletBinding()]
param(
    [string]$ShortcutPath = (Join-Path ([Environment]::GetFolderPath("Desktop")) "BD2HEVC.lnk"),
    [switch]$NoBuild
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $projectRoot "BD2HEVC.exe"
$icon = Join-Path $projectRoot "assets\BD2HEVC.ico"

if (-not $NoBuild -or -not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    & (Join-Path $PSScriptRoot "build-gui-launcher.ps1") -Destination $launcher | Out-Null
}
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) {
    throw "BD2HEVC.exe was not built"
}

$shortcutFullPath = [IO.Path]::GetFullPath($ShortcutPath)
New-Item -ItemType Directory -Path (Split-Path -Parent $shortcutFullPath) -Force | Out-Null
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutFullPath)
$shortcut.TargetPath = $launcher
$shortcut.WorkingDirectory = $projectRoot
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = "BD2HEVC Blu-ray backup converter"
$shortcut.Save()

Get-Item -LiteralPath $shortcutFullPath | Select-Object FullName, Length, LastWriteTime
