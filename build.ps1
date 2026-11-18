# chatsearch 打包脚本：生成 dist\chatsearch.exe（单文件、无控制台）
# 用法：pwsh -File build.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    throw '未找到 .venv，请先执行 uv sync'
}

if (-not (Test-Path 'data\chatsearch.ico')) {
    Write-Host '[build] 生成图标...'
    uv run python -m chatsearch.tray --export-icon
}

Write-Host '[build] PyInstaller 打包中...'
# 直接用 venv 里的解释器调 PyInstaller，不走 `uv run pyinstaller`：
# uv 的 trampoline 在某些版本上会报 "failed to canonicalize script path"，
# 而且它失败时脚本仍会拿上一次的旧 exe 打印哈希，很容易误以为打包成功。
$startedAt = Get-Date
& '.venv\Scripts\python.exe' -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name chatsearch `
    --icon 'data/chatsearch.ico' `
    --paths src `
    --add-data 'src/chatsearch/static;chatsearch/static' `
    launcher.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（exit $LASTEXITCODE）" }

$exe = Get-Item 'dist\chatsearch.exe'
if ($exe.LastWriteTime -lt $startedAt) { throw 'dist\chatsearch.exe 没有更新，打包实际失败了' }
Write-Host ''
Write-Host ("[build] 完成: {0}  ({1} MB)" -f $exe.FullName, [math]::Round($exe.Length / 1MB, 2)) -ForegroundColor Green
Write-Host ("[build] SHA256: {0}" -f (Get-FileHash $exe.FullName -Algorithm SHA256).Hash.ToLower())
