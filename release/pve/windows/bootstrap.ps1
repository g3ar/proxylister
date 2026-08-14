# Prepare the minimal Windows build-template guest after unattended Setup.
#
# Windows Setup invokes this once at the first local administrator logon. All
# third-party payloads come from the generated PROXYLISTER answer ISO after the
# PVE-side provisioner has verified their pinned checksums.

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$LogPath = "C:\proxylister-template-setup.log"
$PythonSha256 = "EDEC09C4853AEAE9AC36EFB8C9F95B6B8E2FEE65EEE56D9767A8B7C69C574403"
$OpenSshSha256 = "DDEC9C53864280759CF9F74791CEFD387100E3946AA849A1C138A4ED1B96B7D9"

Start-Transcript -Path $LogPath -Append

function Find-FileOnCdrom {
    param([Parameter(Mandatory = $true)][string]$RelativePath)

    foreach ($disk in Get-CimInstance Win32_LogicalDisk -Filter "DriveType = 5") {
        $candidate = Join-Path ($disk.DeviceID + "\") $RelativePath
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    throw "Required CD-ROM payload not found: $RelativePath"
}

function Invoke-Installer {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string]$Arguments
    )

    $process = Start-Process -FilePath $FilePath -ArgumentList $Arguments -Wait -PassThru
    if ($process.ExitCode -notin 0, 3010) {
        throw "Installer failed with exit code $($process.ExitCode): $FilePath"
    }
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Template bootstrap requires an elevated administrator session"
}

# Avoid interactive policy and reputation prompts in this isolated build VM.
Set-ExecutionPolicy -ExecutionPolicy Bypass -Scope LocalMachine -Force
New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\System" -Force | Out-Null
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\System" `
    -Name EnableSmartScreen -Type DWord -Value 0
New-Item -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" -Force | Out-Null
Set-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System" `
    -Name LocalAccountTokenFilterPolicy -Type DWord -Value 1

$virtioInstaller = Find-FileOnCdrom "virtio-win-guest-tools.exe"
Write-Output "stage=virtio-install"
Invoke-Installer $virtioInstaller "/quiet /norestart"

$qga = Get-Service -Name "QEMU-GA" -ErrorAction Stop
Set-Service -Name $qga.Name -StartupType Automatic
Start-Service -Name $qga.Name

$openSshInstaller = Find-FileOnCdrom "OpenSSH-Win64-v10.0.0.0.msi"
$actualOpenSshSha256 = (Get-FileHash -LiteralPath $openSshInstaller -Algorithm SHA256).Hash
if ($actualOpenSshSha256 -ne $OpenSshSha256) {
    throw "Pinned Win32-OpenSSH installer checksum mismatch"
}
Write-Output "stage=openssh-install"
Invoke-Installer msiexec.exe "/i `"$openSshInstaller`" ADDLOCAL=Server /qn /norestart"
Write-Output "stage=openssh-configure"

$authorizedKeySource = Find-FileOnCdrom "builder.pub"
$sshDirectory = Join-Path $env:ProgramData "ssh"
$openSshDirectory = Join-Path $env:ProgramFiles "OpenSSH"
$authorizedKeys = Join-Path $sshDirectory "administrators_authorized_keys"
New-Item -ItemType Directory -Path $sshDirectory -Force | Out-Null
Copy-Item -LiteralPath $authorizedKeySource -Destination $authorizedKeys -Force
& icacls.exe $authorizedKeys /inheritance:r /grant "*S-1-5-18:F" /grant "*S-1-5-32-544:F" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Could not set OpenSSH authorized-key permissions"
}

$sshdConfig = Join-Path $sshDirectory "sshd_config"
$config = if (Test-Path -LiteralPath $sshdConfig) {
    Get-Content -LiteralPath $sshdConfig
} else {
    @()
}
$config = $config | Where-Object {
    $_ -notmatch "^\s*#?\s*(PubkeyAuthentication|PasswordAuthentication)\s+" -and
    $_ -notmatch "^\s*#?\s*Subsystem\s+sftp\s+" -and
    $_ -notmatch "^\s*Match Group administrators\s*$" -and
    $_ -notmatch "^\s*AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys\s*$"
}
$config += "PubkeyAuthentication yes"
$config += "PasswordAuthentication no"
$config += "Subsystem sftp sftp-server.exe"
$config += "Match Group administrators"
$config += "       AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys"
Set-Content -LiteralPath $sshdConfig -Value $config -Encoding ascii

& (Join-Path $openSshDirectory "ssh-keygen.exe") -A
if ($LASTEXITCODE -ne 0) {
    throw "OpenSSH host-key generation failed"
}
& (Join-Path $openSshDirectory "sshd.exe") -t
if ($LASTEXITCODE -ne 0) {
    throw "OpenSSH configuration validation failed"
}
Set-Service -Name sshd -StartupType Automatic
Start-Service -Name sshd
if (-not (Get-NetFirewallRule -Name "ProxyLister-OpenSSH" -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name "ProxyLister-OpenSSH" -DisplayName "ProxyLister OpenSSH" `
        -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}

$pythonInstaller = Find-FileOnCdrom "python-3.13.15-amd64.exe"
$actualPythonSha256 = (Get-FileHash -LiteralPath $pythonInstaller -Algorithm SHA256).Hash
if ($actualPythonSha256 -ne $PythonSha256) {
    throw "Pinned Python installer checksum mismatch"
}
Write-Output "stage=python-install"
Invoke-Installer $pythonInstaller "/quiet InstallAllUsers=1 TargetDir=C:\Python313 PrependPath=1 Include_pip=1 Include_test=0 Include_launcher=1"

$python = "C:\Python313\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Pinned Python installation is missing"
}
$pythonVersion = & $python --version 2>&1
if ($LASTEXITCODE -ne 0 -or $pythonVersion -notmatch "^Python 3\.13\.") {
    throw "Unexpected Python version: $pythonVersion"
}

# These clones live only for one build. Freeze the base ISO state and prevent
# surprise background servicing or reboots during a packaging/test run.
New-Item -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" -Force | Out-Null
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" `
    -Name NoAutoUpdate -Type DWord -Value 1
Set-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU" `
    -Name NoAutoRebootWithLoggedOnUsers -Type DWord -Value 1
powercfg.exe /hibernate off
powercfg.exe /change standby-timeout-ac 0
powercfg.exe /change monitor-timeout-ac 0

# Build activity is intentionally trusted inside this disposable lab guest.
if (Get-Command Set-MpPreference -ErrorAction SilentlyContinue) {
    Set-MpPreference -DisableRealtimeMonitoring $true -ErrorAction Continue
    Add-MpPreference -ExclusionPath "C:\proxylister-build", "C:\Users\builder\proxylister" `
        -ErrorAction Continue
}

$ready = [ordered]@{
    template_mode = "ready-state-v1"
    windows_product = (Get-CimInstance Win32_OperatingSystem).Caption
    windows_version = (Get-CimInstance Win32_OperatingSystem).Version
    python = $pythonVersion
    virtio_source = (Split-Path -Leaf $virtioInstaller)
    prepared_utc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
}
$ready | ConvertTo-Json | Set-Content -LiteralPath "C:\proxylister-template-ready.json" -Encoding utf8
Write-Output "stage=ready"

# The template is a single-purpose build appliance. Clones intentionally keep
# its hostname, machine identity, and SSH host key; the PVE build lock permits
# only one Windows build clone at a time. Keeping the prepared state removes
# specialize and OOBE from every disposable clone boot.
Remove-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" `
    -Name AutoAdminLogon, DefaultUserName, DefaultPassword -ErrorAction SilentlyContinue

Write-Output "stage=shutdown"
Stop-Transcript
Stop-Computer -Force
