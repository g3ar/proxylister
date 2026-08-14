# Build and validate the native Windows one-file executable inside a disposable
# clone of the accepted PVE template. The POSIX driver supplies source identity.

param(
    [Parameter(Mandatory = $true)][string]$SourceCommit,
    [Parameter(Mandatory = $true)][ValidateSet("clean", "dirty")][string]$SourceTree
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$Work = Join-Path $Root "release\.work\windows"
$BinRoot = Join-Path $Root "release\bin"
$Bin = Join-Path $BinRoot "windows"
$Venv = Join-Path $Work "venv"
$Dist = Join-Path $Work "artifacts"
$Build = Join-Path $Work "pyinstaller"
$Logs = Join-Path $Work "logs"
$Constraints = Join-Path $PSScriptRoot "constraints.txt"

Remove-Item -LiteralPath $Work -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $Bin -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $Work, $Dist, $Build, $Logs, $BinRoot -Force | Out-Null
$BuildLog = Join-Path $Logs "build.log"
Start-Transcript -Path $BuildLog -Force

try {
    & C:\Python313\python.exe -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "virtual environment creation failed" }
    $Python = Join-Path $Venv "Scripts\python.exe"
    $PyInstaller = Join-Path $Venv "Scripts\pyinstaller.exe"
    $env:PIP_CONSTRAINT = $Constraints

    & $Python -m pip install --upgrade pip setuptools
    if ($LASTEXITCODE -ne 0) { throw "pip bootstrap failed" }
    & $Python -m pip install -e $Root pyinstaller
    if ($LASTEXITCODE -ne 0) { throw "Windows build dependency installation failed" }

    $Expected = Get-Content -LiteralPath $Constraints |
        Where-Object { $_ -notmatch "^\s*(#|$)" } | Sort-Object
    $ResolvedPath = Join-Path $Work "resolved-packages.txt"
    & $Python -m pip freeze --all --exclude-editable | Sort-Object |
        Set-Content -LiteralPath $ResolvedPath -Encoding ascii
    if ($LASTEXITCODE -ne 0) { throw "pip freeze failed" }
    $Resolved = Get-Content -LiteralPath $ResolvedPath
    $Difference = Compare-Object -ReferenceObject $Expected -DifferenceObject $Resolved -CaseSensitive:$false
    if ($Difference) {
        $Difference | Format-Table | Out-String | Write-Error
        throw "resolved Windows packages differ from constraints.txt"
    }

    Push-Location $Root
    try {
        & $Python -m unittest discover -v
        if ($LASTEXITCODE -ne 0) { throw "Windows source test gate failed" }
        & $Python -c "import pathlib, py_compile; [py_compile.compile(str(p), doraise=True) for p in pathlib.Path('src/proxytools').rglob('*.py')]"
        if ($LASTEXITCODE -ne 0) { throw "Windows source compilation gate failed" }
    } finally {
        Pop-Location
    }

    $BuildUtc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    $BuildInfo = Join-Path $Work "proxytools-build.txt"
    @(
        "build_utc=$BuildUtc"
        "source_commit=$SourceCommit"
    ) | Set-Content -LiteralPath $BuildInfo -Encoding ascii
    $env:PROXYTOOLS_BUILD_INFO = $BuildInfo

    & $PyInstaller --noconfirm --clean --distpath $Dist --workpath $Build `
        (Join-Path $Root "release\pyinstaller\proxytools.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller Windows build failed" }

    Copy-Item -LiteralPath (Join-Path $Root "README.md") -Destination (Join-Path $Dist "README.md")
    Copy-Item -LiteralPath (Join-Path $Root "LICENSE") -Destination (Join-Path $Dist "LICENSE")
    $Artifact = Join-Path $Dist "proxytools.exe"
    if (-not (Test-Path -LiteralPath $Artifact)) { throw "PyInstaller did not create proxytools.exe" }
    $Version = (& $Artifact --version 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $Version -notmatch "^[0-9A-Za-z.-]+$") {
        throw "invalid artifact version: $Version"
    }

    $Os = Get-CimInstance Win32_OperatingSystem
    $PipVersion = (& $Python -m pip --version).Split(" ")[1]
    $PyInstallerVersion = (& $PyInstaller --version | Out-String).Trim()
    $ConstraintsSha256 = (Get-FileHash -LiteralPath $Constraints -Algorithm SHA256).Hash.ToLowerInvariant()
    @(
        "artifact=proxytools.exe"
        "version=$Version"
        "source_commit=$SourceCommit"
        "source_tree=$SourceTree"
        "build_utc=$BuildUtc"
        "os=$($Os.Caption) $($Os.Version)"
        "architecture=$([Runtime.InteropServices.RuntimeInformation]::OSArchitecture)"
        "python=$(& $Python --version 2>&1)"
        "pip=$PipVersion"
        "pyinstaller=$PyInstallerVersion"
        "constraints_sha256=$ConstraintsSha256"
    ) | Set-Content -LiteralPath (Join-Path $Dist "MANIFEST.txt") -Encoding ascii

    $ChecksumNames = @("proxytools.exe", "README.md", "LICENSE", "MANIFEST.txt")
    $Checksums = foreach ($Name in $ChecksumNames) {
        $Hash = (Get-FileHash -LiteralPath (Join-Path $Dist $Name) -Algorithm SHA256).Hash.ToLowerInvariant()
        "$Hash  $Name"
    }
    $Checksums | Set-Content -LiteralPath (Join-Path $Dist "SHA256SUMS") -Encoding ascii

    & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File (Join-Path $PSScriptRoot "smoke.ps1") -Artifact $Artifact `
        *> (Join-Path $Logs "smoke.log")
    if ($LASTEXITCODE -ne 0) {
        Get-Content -LiteralPath (Join-Path $Logs "smoke.log") -Tail 160 | Write-Error
        throw "Windows frozen smoke gate failed"
    }

    Move-Item -LiteralPath $Dist -Destination $Bin
    Write-Output "Windows artifact: $Bin\proxytools.exe"
    Write-Output "Manifest: $Bin\MANIFEST.txt"
    Write-Output "Checksums: $Bin\SHA256SUMS"
    Write-Output "Logs: $Logs"
} finally {
    Stop-Transcript -ErrorAction SilentlyContinue
}
