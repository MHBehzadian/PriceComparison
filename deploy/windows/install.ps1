# Installs PriceComparison on Windows Server and starts it in the background.
#
#   Right-click PowerShell > Run as Administrator, then:
#     cd C:\PriceComparison
#     powershell -ExecutionPolicy Bypass -File deploy\windows\install.ps1
#
#   Options:
#     -Port 8090                     port of the panel
#     -DataDir C:\ProgramData\PriceComparison   where products and prices are kept
#     -Wheels C:\wheels              install packages from this folder (server without internet)
#     -TrustedProxy 192.168.1.20     IP of the reverse proxy that publishes the panel on the internet
#
# Run it again after updating the code: it updates packages and restarts the panel.
# Data in -DataDir is never touched.
#
# The panel runs as a scheduled task under the SYSTEM account: it starts with
# Windows, keeps running after you log off from Remote Desktop, and is started
# again within 5 minutes if it ever stops.

param(
    [int]$Port = 8090,
    [string]$DataDir = "$env:ProgramData\PriceComparison",
    [string]$Wheels = "",
    [string]$TrustedProxy = ""
)

$ErrorActionPreference = "Stop"
$TaskName = "PriceComparison"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Venv = Join-Path $Root "venv"
$PyVenv = Join-Path $Venv "Scripts\python.exe"
$LogFile = Join-Path $DataDir "panel.log"

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host ""; Write-Host "ERROR: $text" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- checks
$identity = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "Run PowerShell as Administrator (right-click > Run as administrator)."
}
if (-not (Test-Path (Join-Path $Root "pricecomparison\__main__.py"))) {
    Fail "Run this script from the PriceComparison folder (deploy\windows\install.ps1 inside it)."
}

Step "Looking for Python 3.10 or newer"
# "python" on a fresh Windows can be a Microsoft Store shortcut that does not
# run anything, so every candidate is asked for its version
$PyExe = $null
$PyArgs = @()
foreach ($spec in @("py|-3", "python")) {
    $parts = $spec.Split("|")
    $exe = $parts[0]
    $extra = @($parts | Select-Object -Skip 1)
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
    try {
        $out = & $exe @extra -c "import sys; print(int(sys.version_info >= (3, 10)))" 2>$null
    } catch { $out = "" }
    if ("$out".Trim() -eq "1") { $PyExe = $exe; $PyArgs = $extra; break }
}
if (-not $PyExe) {
    Fail "Python 3.10+ was not found. Install Python 3.12 from https://www.python.org/downloads/windows/ (tick 'Add python.exe to PATH' and 'Install for all users'), open a new PowerShell and run this script again."
}
$version = & $PyExe @PyArgs -c "import sys; print(sys.version.split()[0])"
Write-Host "Python $version"

# ---------------------------------------------------- stop a running copy
Step "Stopping the panel if it is running"
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}
# the task's python may outlive the task; stop only our own python
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and ($_.ExecutablePath -ieq $PyVenv) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $owner = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    Fail "Port $Port is already used by '$($owner.ProcessName)' (PID $($listener.OwningProcess)). Nothing was changed. Pick another port: -Port 9091"
}

# --------------------------------------------------------------- install
Step "Creating the Python environment in $Venv"
if (-not (Test-Path $PyVenv)) {
    & $PyExe @PyArgs -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the Python environment." }
}

Step "Installing packages"
$pipArgs = @("-m", "pip", "install", "--disable-pip-version-check", "-r", (Join-Path $Root "requirements.txt"))
if ($Wheels) {
    if (-not (Test-Path $Wheels)) { Fail "Folder $Wheels does not exist." }
    $pipArgs += @("--no-index", "--find-links", $Wheels)
}
& $PyVenv @pipArgs
if ($LASTEXITCODE -ne 0) {
    Fail "Installing packages failed. If this server has no internet, build a wheels folder on another PC (see README) and run again with -Wheels C:\wheels"
}

# ------------------------------------------------------------------ data
Step "Preparing the data folder $DataDir"
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
# only Administrators and SYSTEM may read it (it holds the login hash)
& icacls $DataDir /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null

$Password = $null
$authFile = Join-Path $DataDir "auth.json"
$hasLogin = (Test-Path $authFile) -and ((Get-Content $authFile -Raw) -match '"hash"')
if (-not $hasLogin) {
    $chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789".ToCharArray()
    $Password = -join (1..16 | ForEach-Object { $chars | Get-Random })
    & $PyVenv -m pricecomparison --data $DataDir passwd --username admin --password $Password
    if ($LASTEXITCODE -ne 0) { Fail "Could not set the admin password." }
}

# ------------------------------------------------------- background task
Step "Registering the background task '$TaskName'"
$arguments = "-m pricecomparison --data `"$DataDir`" --log-file `"$LogFile`" serve --host 0.0.0.0 --port $Port"
if ($TrustedProxy) { $arguments += " --trusted-proxy $TrustedProxy" }
$action = New-ScheduledTaskAction -Execute $PyVenv -Argument $arguments -WorkingDirectory $Root
$atBoot = New-ScheduledTaskTrigger -AtStartup
# every 5 minutes: starts the panel again if it has stopped (a running copy is left alone)
$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($atBoot, $watchdog) -Settings $settings `
    -Principal $principal -Description "PriceComparison panel on port $Port" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Step "Opening port $Port in Windows Firewall"
Get-NetFirewallRule -DisplayName "PriceComparison*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "PriceComparison $Port" -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow -Profile Any | Out-Null

# ------------------------------------------------------------------ check
Step "Waiting for the panel to answer"
$up = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/session" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { $up = $true; break }
    } catch { }
}
if (-not $up) {
    Write-Host ""
    Write-Host "The panel did not answer. Last lines of $LogFile :" -ForegroundColor Red
    if (Test-Path $LogFile) { Get-Content $LogFile -Tail 25 }
    Fail "See the log above. Task status: Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo"
}

$ips = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "127.*" -and $_.IPAddress -notlike "169.254.*" } |
    Select-Object -ExpandProperty IPAddress

Write-Host ""
Write-Host "PriceComparison is running." -ForegroundColor Green
Write-Host "  On this server:   http://localhost:$Port"
foreach ($ip in $ips) { Write-Host "  From the network: http://${ip}:$Port" }
if ($Password) {
    Write-Host ""
    Write-Host "  username: admin"
    Write-Host "  password: $Password" -ForegroundColor Yellow
    Write-Host "  (write it down; change it later with: venv\Scripts\python -m pricecomparison --data `"$DataDir`" passwd)"
}
Write-Host ""
Write-Host "  Log file: $LogFile"
