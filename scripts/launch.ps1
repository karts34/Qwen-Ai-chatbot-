param(
    [switch]$Setup,
    [switch]$Stop,
    [switch]$DeepSeek,
    [ValidateRange(1, 65535)][int]$Port = 8080,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
if ($Stop -and ($Setup -or $DeepSeek)) { throw 'Use -Stop on its own, without -Setup or -DeepSeek.' }

if ($Stop) {
    & (Join-Path $PSScriptRoot 'stop.ps1')
    return
}

. (Join-Path $PSScriptRoot 'common.ps1')

function Test-InstalledModel([string]$Name, [string]$Tag) {
    $manifestRoot = Join-Path $ProjectRoot '.models\manifests\registry.ollama.ai\library'
    $manifestPath = Join-Path (Join-Path $manifestRoot $Name) $Tag
    try {
        if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { return $false }
        $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
        if ($manifest.schemaVersion -ne 2 -or -not $manifest.config -or -not $manifest.layers) {
            return $false
        }
        $blobs = @($manifest.config) + @($manifest.layers)
        foreach ($blob in $blobs) {
            $digest = [string]$blob.digest
            if ($digest -cnotmatch '^sha256:([0-9a-f]{64})$') { return $false }
            $blobName = 'sha256-' + $Matches[1]
            [long]$expectedSize = 0
            if (-not [long]::TryParse([string]$blob.size, [ref]$expectedSize) -or $expectedSize -le 0) {
                return $false
            }
            $blobPath = Join-Path (Join-Path $ProjectRoot '.models\blobs') $blobName
            $file = Get-Item -LiteralPath $blobPath -ErrorAction Stop
            if ($file.PSIsContainer -or $file.Length -ne $expectedSize) { return $false }
        }
        return $true
    } catch {
        return $false
    }
}

function Get-MissingInstallItems {
    if (-not (Test-Path -LiteralPath $OllamaExe -PathType Leaf)) { 'local model runtime' }
    foreach ($model in @('qwen3:1.7b', 'llama3.2:1b', 'gemma3:1b')) {
        $parts = $model.Split(':')
        if (-not (Test-InstalledModel $parts[0] $parts[1])) { $model }
    }
    $localModel = Join-Path $ProjectRoot 'qwen3-4b-thinking-2507.Q4_K_M.gguf'
    if ((Test-Path -LiteralPath $localModel -PathType Leaf) -and
        -not (Test-InstalledModel 'qwen3-4b-thinking-local' 'latest')) {
        'existing Qwen 4B import'
    }
}

# File names and sizes provide a quick offline readiness check. Full digest
# verification belongs to setup, so normal launches do not reread model weights.
$missing = @(Get-MissingInstallItems)
if ($Setup -or $missing.Count -gt 0) {
    if ($missing.Count -gt 0) {
        Write-Host ('Installing missing or incomplete components: ' + ($missing -join ', '))
    }
    & (Join-Path $PSScriptRoot 'setup.ps1')
    $remaining = @(Get-MissingInstallItems)
    if ($remaining.Count -gt 0) {
        throw ('Setup did not finish installing: ' + ($remaining -join ', ') + '. Run start.bat -Setup to resume.')
    }
}

if ($DeepSeek) {
    & (Join-Path $PSScriptRoot 'install-deepseek.ps1')
}

& (Join-Path $PSScriptRoot 'start.ps1') -Port $Port -NoBrowser:$NoBrowser
