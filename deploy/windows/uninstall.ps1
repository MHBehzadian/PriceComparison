# Stops PriceComparison and removes its background task and firewall rule.
# Products and prices in the data folder are kept unless -RemoveData is given.
#
#   powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall.ps1 [-RemoveData]

param(
    [string]$DataDir = "$env:ProgramData\PriceComparison",
    [switch]$RemoveData
)

$ErrorActionPreference = "Stop"
$TaskName = "PriceComparison"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$PyVenv = Join-Path $Root "venv\Scripts\python.exe"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Background task removed."
}
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.ExecutablePath -and ($_.ExecutablePath -ieq $PyVenv) } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-NetFirewallRule -DisplayName "PriceComparison*" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
Write-Host "Firewall rule removed."

if ($RemoveData) {
    Remove-Item -Recurse -Force $DataDir -ErrorAction SilentlyContinue
    Write-Host "Data folder $DataDir deleted."
} else {
    Write-Host "Data kept in $DataDir"
}
