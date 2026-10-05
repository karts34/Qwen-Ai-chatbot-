param([int]$Port = 8080, [switch]$NoBrowser)
. (Join-Path $PSScriptRoot 'common.ps1')
if ($Port -lt 1 -or $Port -gt 65535) { throw 'Port must be between 1 and 65535.' }
$pythonPath = Get-ProjectPython
Start-ProjectOllama
$url = "http://127.0.0.1:$Port"
$ready = $false
try {
    $status = Invoke-RestMethod -Uri "$url/api/status" -TimeoutSec 2
    $ready = $status.service -eq 'local-chat'
} catch { }
if (-not $ready) {
    $listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($listener) { throw "Port $Port is occupied. Start with scripts\start.ps1 -Port 8081 to use another port." }
    $arguments = @(('"' + (Join-Path $ProjectRoot 'app.py') + '"'), '--port', "$Port")
    $process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $ProjectRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogRoot 'chat.stdout.log') -RedirectStandardError (Join-Path $LogRoot 'chat.stderr.log') -PassThru
    Save-ProjectProcess $process 'chat'
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        try {
            $status = Invoke-RestMethod -Uri "$url/api/status" -TimeoutSec 2
            if ($status.service -eq 'local-chat') { $ready = $true; break }
        } catch { }
        $process.Refresh()
        if ($process.HasExited) { throw "Chatbot exited. Read $LogRoot\chat.stderr.log." }
        Start-Sleep -Milliseconds 250
    }
    if (-not $ready) { throw "Chatbot did not become ready. Read $LogRoot\chat.stderr.log." }
}
Write-Host "Local chatbot running at $url"
Write-Host 'Installed models work without internet. Run start.bat -Stop when finished.'
if (-not $NoBrowser) { Start-Process $url }
