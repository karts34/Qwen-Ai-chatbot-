param()
. (Join-Path $PSScriptRoot 'common.ps1')
$pythonPath = Get-ProjectPython
Set-Location -LiteralPath $ProjectRoot
Start-ProjectOllama
Write-Host 'Installing DeepSeek-R1 Distill Qwen 1.5B from Hugging Face...'
& $pythonPath (Join-Path $ProjectRoot 'tools\download_huggingface.py')
if ($LASTEXITCODE -ne 0) { throw 'DeepSeek download failed. Run start.bat -DeepSeek to resume.' }
& $OllamaExe create 'deepseek-r1-hf:1.5b' -f (Join-Path $ProjectRoot 'Modelfile.deepseek-hf')
if ($LASTEXITCODE -ne 0) { throw 'DeepSeek import failed. Run start.bat -DeepSeek to retry.' }
Write-Host 'DeepSeek installed. Select deepseek-r1-hf:1.5b in the chat page to use it offline.'
