param(
    [string]$BotExecutable = (Get-Command scooters-codex-telegram-bot).Source,
    [string]$ConfigFile = "$env:APPDATA\scooters-codex-telegram-bot\.env"
)

$ErrorActionPreference = "Stop"
$TaskName = "Scooters Codex Telegram Bot"
$Arguments = "--config `"$ConfigFile`""
$Action = New-ScheduledTaskAction -Execute $BotExecutable -Argument $Arguments
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -RestartCount 10 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Private Telegram bridge to Codex App Server" `
    -Force

Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed and started: $TaskName"
