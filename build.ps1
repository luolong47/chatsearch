# chatsearch 打包脚本：生成 dist\chatsearch.exe（单文件、无控制台）
# 用法：pwsh -File build.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    throw '未找到 .venv，请先执行 uv sync'
}

# 版本号有两处：pyproject.toml 的 version（给 scoop 与依赖元数据）
# 和 config.py 的 APP_VERSION（页面上的版本徽章）。忘同步就会徽章显示旧版本，
# 所以打包前直接拦下来。
function Get-VersionFrom($path, $pattern) {
    $hit = Select-String -Path $path -Pattern $pattern | Select-Object -First 1
    if (-not $hit) { return $null }
    return $hit.Matches[0].Groups[1].Value
}
$pyVer = Get-VersionFrom 'pyproject.toml' '^version\s*=\s*"([^"]+)"'
$cfgVer = Get-VersionFrom 'src\chatsearch\config.py' '^APP_VERSION\s*=\s*"([^"]+)"'
if (-not $pyVer -or $pyVer -ne $cfgVer) {
    throw "版本号不一致：pyproject.toml=$pyVer，config.py APP_VERSION=$cfgVer"
}
Write-Host "[build] 版本 $pyVer"

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
