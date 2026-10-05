$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$RuntimeRoot = Join-Path $ProjectRoot '.runtime'
$OllamaExe = Join-Path $RuntimeRoot 'ollama\ollama.exe'
$LogRoot = Join-Path $RuntimeRoot 'logs'
New-Item -ItemType Directory -Path $RuntimeRoot, $LogRoot, (Join-Path $ProjectRoot '.models') -Force | Out-Null

function Get-ProjectPython {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $result = & $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $result -and (Test-Path -LiteralPath $result)) { return $result }
    }
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notlike '*WindowsApps*') { return $command.Source }
    throw 'Python 3 is required. Install Python 3.11 or newer from https://www.python.org/downloads/windows/.'
}

function Set-LocalModelEnvironment {
    $env:OLLAMA_HOST = '127.0.0.1:11434'
    $env:OLLAMA_MODELS = Join-Path $ProjectRoot '.models'
    $env:OLLAMA_NO_CLOUD = '1'
    $env:OLLAMA_MAX_LOADED_MODELS = '1'
    $env:OLLAMA_NUM_PARALLEL = '1'
    $env:OLLAMA_CONTEXT_LENGTH = '4096'
    $env:NO_PROXY = 'localhost,127.0.0.1,::1'
    $env:PYTHONUNBUFFERED = '1'
}

function Save-ProjectProcess($Process, [string]$Name) {
    $record = [pscustomobject]@{
        pid = $Process.Id
        started = $Process.StartTime.ToUniversalTime().Ticks.ToString()
        path = $Process.Path
    }
    $record | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $RuntimeRoot "$Name-process.json") -Encoding UTF8
}

function Get-OllamaVersion {
    try { return Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/version' -TimeoutSec 2 } catch { return $null }
}

function Start-ProjectOllama {
    Set-LocalModelEnvironment
    if (-not (Test-Path -LiteralPath $OllamaExe)) { throw 'Run start.bat to install the local model runtime, or start.bat -Setup to repair it.' }
    $version = Get-OllamaVersion
    if ($version) {
        $listener = Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        $existing = if ($listener) { Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue }
        if (-not $existing -or $existing.Path -ne $OllamaExe) {
            throw 'Port 11434 is occupied by a different Ollama installation. Close that installation before starting this project.'
        }
        return
    }
    $process = Start-Process -FilePath $OllamaExe -ArgumentList 'serve' -WorkingDirectory (Split-Path -Parent $OllamaExe) -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogRoot 'ollama.stdout.log') -RedirectStandardError (Join-Path $LogRoot 'ollama.stderr.log') -PassThru
    Save-ProjectProcess $process 'ollama'
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if (Get-OllamaVersion) { return }
        $process.Refresh()
        if ($process.HasExited) { throw "Ollama exited. Read $LogRoot\ollama.stderr.log." }
        Start-Sleep -Milliseconds 500
    }
    throw "Ollama did not become ready. Read $LogRoot\ollama.stderr.log."
}
