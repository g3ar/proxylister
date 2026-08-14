# Run deterministic smoke checks against the frozen Windows executable.

param(
    [Parameter(Mandatory = $true)]
    [string]$Artifact
)

$ErrorActionPreference = "Stop"
$Artifact = (Resolve-Path -LiteralPath $Artifact).Path
if (-not (Test-Path -LiteralPath $Artifact -PathType Leaf)) {
    throw "Windows artifact is missing: $Artifact"
}

$SmokeRoot = Join-Path $env:TEMP ("proxytools-windows-smoke-" + [guid]::NewGuid().ToString("N"))
$Runtime = Join-Path $SmokeRoot "runtime"
$ReadOnly = Join-Path $SmokeRoot "read-only"
$OriginalPath = $env:PATH
$OriginalHome = $env:HOME
$OriginalUserProfile = $env:USERPROFILE

function Invoke-ProxyTools {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $output = & $Executable @Arguments 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        throw "proxytools.exe failed ($LASTEXITCODE): $($Arguments -join ' ')`n$output"
    }
    return $output.TrimEnd()
}

try {
    New-Item -ItemType Directory -Path $Runtime, $ReadOnly | Out-Null
    $Executable = Join-Path $Runtime "proxytools.exe"
    Copy-Item -LiteralPath $Artifact -Destination $Executable

    # The executable must not find Python or the source checkout through PATH.
    $env:PATH = "$env:SystemRoot\System32;$env:SystemRoot"
    $env:HOME = Join-Path $SmokeRoot "home"
    $env:USERPROFILE = $env:HOME

    $version = Invoke-ProxyTools $Executable @("--version")
    if ([string]::IsNullOrWhiteSpace($version)) { throw "empty version output" }
    $about = Invoke-ProxyTools $Executable @("--about")
    if ($about -notmatch [regex]::Escape("Proxy Tools $version")) { throw "About version mismatch" }
    if ($about -notmatch "Build date: \d{4}-\d{2}-\d{2}T") { throw "Build date missing" }
    if ($about -notmatch "Source commit: [0-9a-f]{40}") { throw "Source commit missing" }
    if ((Invoke-ProxyTools $Executable @("--help")) -notmatch "Usage:") { throw "root help failed" }
    if ((Invoke-ProxyTools $Executable @("list", "--help")) -notmatch "--max-latency") { throw "list help failed" }

    $Config = Join-Path $Runtime "proxytools.conf"
    if (-not (Test-Path -LiteralPath $Config)) { throw "default config was not created" }
    Add-Content -LiteralPath $Config -Value "`n# preserved by frozen smoke"
    if ((Invoke-ProxyTools $Executable @("monitor", "--help")) -notmatch "--max-latency") {
        throw "monitor help failed"
    }
    if (-not (Select-String -LiteralPath $Config -SimpleMatch "# preserved by frozen smoke" -Quiet)) {
        throw "existing config was overwritten"
    }

    $clear = Invoke-ProxyTools $Executable @("--clear")
    if ($clear -notmatch "Removed|already clean") { throw "clear smoke failed" }
    foreach ($generated in ".venv", "geodb", "proxydb") {
        if (Test-Path -LiteralPath (Join-Path $Runtime $generated)) {
            throw "clear retained generated path: $generated"
        }
    }

    $ReadOnlyExecutable = Join-Path $ReadOnly "proxytools.exe"
    Copy-Item -LiteralPath $Artifact -Destination $ReadOnlyExecutable
    # Deny creation in the directory itself without inheriting the deny ACE to
    # proxytools.exe; the loader must still be able to execute the artifact.
    & icacls.exe $ReadOnly /deny "${env:USERNAME}:(WD,AD)" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "could not create read-only smoke directory" }
    $ReadOnlyStdout = Join-Path $SmokeRoot "read-only.stdout.log"
    $ReadOnlyStderr = Join-Path $SmokeRoot "read-only.stderr.log"
    $ReadOnlyProcess = Start-Process -FilePath $ReadOnlyExecutable `
        -ArgumentList "list", "--help" -Wait -PassThru `
        -RedirectStandardOutput $ReadOnlyStdout -RedirectStandardError $ReadOnlyStderr
    $readOnlyOutput = ((Get-Content -LiteralPath $ReadOnlyStdout -Raw -ErrorAction SilentlyContinue) +
        (Get-Content -LiteralPath $ReadOnlyStderr -Raw -ErrorAction SilentlyContinue))
    if ($ReadOnlyProcess.ExitCode -eq 0) {
        throw "proxytools unexpectedly wrote config in a read-only directory"
    }
    if ($readOnlyOutput -notmatch "configuration error: cannot create") {
        throw "read-only failure was not reported clearly: $readOnlyOutput"
    }

    Write-Output "Windows frozen smoke tests passed."
} finally {
    $env:PATH = $OriginalPath
    $env:HOME = $OriginalHome
    $env:USERPROFILE = $OriginalUserProfile
    if (Test-Path -LiteralPath $ReadOnly) {
        & icacls.exe $ReadOnly /remove:d $env:USERNAME /t /c | Out-Null
        & icacls.exe $ReadOnly /reset /t /c | Out-Null
    }
    Remove-Item -LiteralPath $SmokeRoot -Recurse -Force -ErrorAction SilentlyContinue
}
