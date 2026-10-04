$ErrorActionPreference = 'Stop'
$taskRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$taskPython = (Get-Command pythonw.exe -ErrorAction Stop).Source
$taskStartup = [Environment]::GetFolderPath('Startup')
$taskVbsPath = Join-Path $taskStartup 'TelegramGiveawayBot.vbs'
$taskCommand = '"' + $taskPython + '" "' + (Join-Path $taskRoot 'run.py') + '"'
$taskVbs = 'Set shell = CreateObject("WScript.Shell")' + "`r`n" +
    'shell.CurrentDirectory = "' + $taskRoot.Replace('"', '""') + '"' + "`r`n" +
    'shell.Run "' + $taskCommand.Replace('"', '""') + '", 0, False' + "`r`n"
Set-Content -LiteralPath $taskVbsPath -Value $taskVbs -Encoding Unicode
Write-Output 'Автозапуск при входе в Windows установлен.'
