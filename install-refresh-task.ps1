# Run in a regular PowerShell session under the account that uses the directory.
$ErrorActionPreference = 'Stop'
$taskName = 'Contract Intelligence - CanadaBuys refresh'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    throw "Task already exists. Start it or edit its schedule in Task Scheduler."
}
$python = if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3 -c "import sys; print(sys.executable)"
} else {
    & python -c "import sys; print(sys.executable)"
}
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) {
    throw 'Could not resolve the Python interpreter.'
}
$identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute $python -Argument '-m ci refresh --feed all' -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Hours 6)
$principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
    -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host "Started $taskName. Check Data Health for the recorded import results."
