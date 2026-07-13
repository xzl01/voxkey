$ErrorActionPreference = 'Continue'
# Ensure `bash` (used by Tauri's beforeBuildCommand) resolves.
$env:PATH = 'C:\Program Files\Git\bin;' + $env:PATH
$env:BUNDLE_PYTHON = '1'
$APP_DIR = 'C:\Users\xzl01\Dev\voxkey\front-end\desktop-ui'
Set-Location $APP_DIR

# Tauri passes localePath verbatim as `-loc` to candle/light, which run from a
# CWD where a relative path does not resolve (os error 3). The committed config
# keeps a repo-relative path (portable), so rewrite it to an absolute path for
# the build, then restore it afterwards so the repo stays clean.
# IMPORTANT: use [IO.File]::WriteAllText (UTF-8, no BOM) — Set-Content's default
# encoding is the system ANSI codepage (GBK here) and would corrupt the JSON.
$CONF = Join-Path $APP_DIR 'src-tauri\tauri.conf.json'
$REL = 'src-tauri/wix-locale.wxl'
# Use forward slashes: backslashes are JSON escape chars and would make the
# config invalid ("invalid escape"); candle/light accept forward slashes on Windows.
$ABS = (Resolve-Path (Join-Path $APP_DIR 'src-tauri\wix-locale.wxl')).Path.Replace('\', '/')
$Original = [System.IO.File]::ReadAllText($CONF)
[System.IO.File]::WriteAllText($CONF, $Original.Replace($REL, $ABS))

try {
    pnpm tauri build *> build.log
} finally {
    [System.IO.File]::WriteAllText($CONF, $Original)
}
