param(
    [string[]]$Models = @('qwen3:1.7b', 'llama3.2:1b', 'gemma3:1b'),
    [switch]$SkipLocalImport,
    [switch]$UseOllamaPull
)
. (Join-Path $PSScriptRoot 'common.ps1')
$pythonPath = Get-ProjectPython
Set-Location -LiteralPath $ProjectRoot
if (-not (Test-Path -LiteralPath $OllamaExe)) {
    Write-Host 'Installing the official portable Ollama runtime in the project folder...'
    $metadataPath = Join-Path $RuntimeRoot 'ollama-release.json'
    if (-not (Test-Path -LiteralPath $metadataPath)) {
        $release = Invoke-RestMethod -Uri 'https://api.github.com/repos/ollama/ollama/releases/latest'
        $asset = $release.assets | Where-Object name -eq 'ollama-windows-amd64.zip' | Select-Object -First 1
        if (-not $asset) { throw 'Official portable Windows release was not found.' }
        [pscustomobject]@{version=$release.tag_name; url=$asset.browser_download_url; size=$asset.size; digest=$asset.digest} | ConvertTo-Json | Set-Content -LiteralPath $metadataPath -Encoding UTF8
    }
    & $pythonPath (Join-Path $ProjectRoot 'tools\download_ollama.py')
    if ($LASTEXITCODE -ne 0) { throw 'Runtime download failed. Rerun start.bat -Setup to resume.' }
    Write-Host 'Extracting the verified runtime...'
    Expand-Archive -LiteralPath (Join-Path $RuntimeRoot 'downloads\ollama-windows-amd64.zip') -DestinationPath (Join-Path $RuntimeRoot 'ollama') -Force
}
Start-ProjectOllama
foreach ($model in $Models) {
    if ($model -match '(?i)cloud|https?://') { throw 'Use a local model tag. Cloud models are disabled for this project.' }
}
if ($UseOllamaPull) {
    foreach ($model in $Models) {
        Write-Host "Installing $model locally..."
        & $OllamaExe pull $model
        if ($LASTEXITCODE -ne 0) { throw "Could not install $model. Rerun start.bat -Setup to resume the download." }
    }
} else {
    Write-Host 'Installing local models with small resumable downloads...'
    & $pythonPath (Join-Path $ProjectRoot 'tools\download_models.py') --models $Models
    if ($LASTEXITCODE -ne 0) { throw 'Model download failed. Rerun start.bat -Setup to resume the verified download chunks.' }
}
if (-not $SkipLocalImport -and (Test-Path -LiteralPath (Join-Path $ProjectRoot 'qwen3-4b-thinking-2507.Q4_K_M.gguf'))) {
    Write-Host 'Importing your existing Qwen 4B Thinking model...'
    & $OllamaExe create 'qwen3-4b-thinking-local' -f (Join-Path $ProjectRoot 'Modelfile.qwen-local')
    if ($LASTEXITCODE -ne 0) { throw 'Local Qwen import failed. The original GGUF file has been preserved.' }
}
Write-Host 'Models installed. Double-click start.bat to chat offline.'
& $OllamaExe list
