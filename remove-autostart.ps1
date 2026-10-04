$taskStartup = [Environment]::GetFolderPath('Startup')
$taskVbsPath = Join-Path $taskStartup 'TelegramGiveawayBot.vbs'
if (Test-Path -LiteralPath $taskVbsPath) {
    Remove-Item -LiteralPath $taskVbsPath
}
Write-Output 'Автозапуск отключён.'
