$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskPython = (Get-Command pythonw.exe -ErrorAction Stop).Source
Start-Process -FilePath $taskPython -ArgumentList ('"' + (Join-Path $taskRoot 'run.py') + '"') -WorkingDirectory $taskRoot -WindowStyle Hidden
Write-Output 'Бот запускается в фоне. Диагностика: logs\bot.log'
