$ErrorActionPreference = 'Stop'
$taskScript = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'run.py'))
$taskProcesses = Get-CimInstance Win32_Process | Where-Object {
    $_.Name -in @('python.exe', 'pythonw.exe') -and
    $_.CommandLine -and $_.CommandLine.Contains('"' + $taskScript + '"')
}
foreach ($taskProcess in $taskProcesses) {
    Stop-Process -Id $taskProcess.ProcessId -ErrorAction SilentlyContinue
}
Write-Output 'Бот остановлен. Автозапуск при следующем входе в Windows остаётся включён.'
