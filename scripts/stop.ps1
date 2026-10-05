. (Join-Path $PSScriptRoot 'common.ps1')
foreach ($name in @('chat', 'ollama')) {
    $recordPath = Join-Path $RuntimeRoot "$name-process.json"
    if (-not (Test-Path -LiteralPath $recordPath)) { continue }
    $record = Get-Content -Raw -LiteralPath $recordPath | ConvertFrom-Json
    $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue
    if ($process -and $process.Path -eq $record.path -and $process.StartTime.ToUniversalTime().Ticks.ToString() -eq $record.started) {
        Stop-Process -Id $process.Id
        Write-Host "Stopped project $name."
    }
    Remove-Item -LiteralPath $recordPath
}
