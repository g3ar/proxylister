# Run a bounded network-dependent list check against the frozen Windows binary.
# Interactive TUI and browser acceptance remain manual release checks.

param(
    [Parameter(Mandatory = $true)][string]$Artifact,
    [int]$TimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"
$Artifact = (Resolve-Path -LiteralPath $Artifact).Path
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$Logs = Join-Path $Root "release\.work\windows\logs"
$LiveRoot = Join-Path $env:TEMP ("proxytools-windows-live-" + [guid]::NewGuid().ToString("N"))
$Runtime = Join-Path $LiveRoot "runtime"
$Stdout = Join-Path $Logs "live-list.log"
$Stderr = Join-Path $Logs "live-list-error.log"

try {
    New-Item -ItemType Directory -Path $Runtime, $Logs -Force | Out-Null
    $Executable = Join-Path $Runtime "proxytools.exe"
    Copy-Item -LiteralPath $Artifact -Destination $Executable
    Remove-Item -LiteralPath $Stdout, $Stderr -Force -ErrorAction SilentlyContinue

    $StartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $StartInfo.FileName = $Executable
    $StartInfo.Arguments = "list"
    $StartInfo.UseShellExecute = $false
    $StartInfo.CreateNoWindow = $true
    $StartInfo.RedirectStandardOutput = $true
    $StartInfo.RedirectStandardError = $true
    $Process = New-Object System.Diagnostics.Process
    $Process.StartInfo = $StartInfo
    if (-not $Process.Start()) { throw "Windows live list smoke could not start" }
    $StdoutTask = $Process.StandardOutput.ReadToEndAsync()
    $StderrTask = $Process.StandardError.ReadToEndAsync()
    if (-not $Process.WaitForExit($TimeoutSeconds * 1000)) {
        $Process.Kill()
        throw "Windows live list smoke timed out after $TimeoutSeconds seconds"
    }
    $Process.WaitForExit()
    $ExitCode = $Process.ExitCode
    $StdoutText = $StdoutTask.GetAwaiter().GetResult()
    $StderrText = $StderrTask.GetAwaiter().GetResult()
    $StdoutText | Set-Content -LiteralPath $Stdout -Encoding utf8
    $StderrText | Set-Content -LiteralPath $Stderr -Encoding utf8
    if ($ExitCode -ne 0) {
        $details = @()
        if (Test-Path -LiteralPath $Stdout) { $details += Get-Content -LiteralPath $Stdout -Tail 120 }
        if (Test-Path -LiteralPath $Stderr) { $details += Get-Content -LiteralPath $Stderr -Tail 120 }
        throw "Windows live list smoke failed with exit code ${ExitCode}:`n$($details -join "`n")"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Runtime "geodb\geoip.mmdb"))) {
        throw "Windows live smoke did not create the GeoIP database"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Runtime "working_proxies.txt"))) {
        throw "Windows live smoke did not create working_proxies.txt"
    }
    Write-Output "Windows live frozen smoke tests passed."
} finally {
    Remove-Item -LiteralPath $LiveRoot -Recurse -Force -ErrorAction SilentlyContinue
}
