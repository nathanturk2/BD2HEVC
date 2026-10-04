[CmdletBinding()]
param([string]$Destination)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $PSScriptRoot "BD2HEVCLauncher.cs"
$icon = Join-Path $projectRoot "assets\BD2HEVC.ico"
if (-not $Destination) { $Destination = Join-Path $projectRoot "BD2HEVC.exe" }
$destinationPath = [IO.Path]::GetFullPath($Destination)
$compiler = @(
    (Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"),
    (Join-Path $env:WINDIR "Microsoft.NET\Framework\v4.0.30319\csc.exe")
) | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $compiler) { throw "The Windows .NET C# compiler was not found" }
if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) { throw "Build the GUI icon first: $icon" }
$temporary = Join-Path ([IO.Path]::GetTempPath()) ("BD2HEVC-launcher-" + [guid]::NewGuid() + ".exe")
try {
    & $compiler /nologo /target:winexe /optimize+ "/win32icon:$icon" /reference:System.Windows.Forms.dll "/out:$temporary" $source
    if ($LASTEXITCODE -ne 0) { throw "The C# compiler failed with exit code $LASTEXITCODE" }
    New-Item -ItemType Directory -Path (Split-Path -Parent $destinationPath) -Force | Out-Null
    Copy-Item -LiteralPath $temporary -Destination $destinationPath -Force
}
finally { Remove-Item -LiteralPath $temporary -Force -ErrorAction SilentlyContinue }
Get-Item -LiteralPath $destinationPath | Select-Object FullName, Length, LastWriteTime
