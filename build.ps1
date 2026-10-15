# Build dist\mvdl.exe and dist\mvdl-win64.zip (mvdl.exe + ffmpeg.exe + ffprobe.exe + deno.exe).
#   powershell -ExecutionPolicy Bypass -File build.ps1            # everything
#   powershell -ExecutionPolicy Bypass -File build.ps1 -NoTools   # exe only (no ffmpeg / deno download)
# Needs Python 3.11+ on PATH (winget install Python.Python.3.12).
param([switch]$NoTools)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'  # Invoke-WebRequest is very slow with the progress bar
Set-Location $PSScriptRoot

if (-not (Test-Path .venv-build)) { python -m venv .venv-build }
$py = '.venv-build\Scripts\python.exe'
& $py -m pip install --upgrade pip
& $py -m pip install --upgrade -r requirements.txt pyinstaller
if ($LASTEXITCODE) { throw 'pip install failed' }

& $py tools/fetch_webview2.py  # WebView2 SDK assemblies for the built-in browser
if ($LASTEXITCODE) { throw 'fetch_webview2 failed' }

& $py -m PyInstaller --noconfirm --clean --onefile --windowed --name mvdl `
    --collect-all yt_dlp_ejs --collect-all pythonnet --collect-all clr_loader `
    --add-data 'mvdl/lib;mvdl/lib' --add-data 'mvdl/ui;mvdl/ui' mvdl.py
if ($LASTEXITCODE) { throw 'PyInstaller failed' }

if (-not $NoTools) {
    $tmp = Join-Path $env:TEMP "mvdl-build-$(Get-Random)"
    New-Item -ItemType Directory $tmp | Out-Null
    try {
        # ffmpeg: merges video + audio, joins HLS / DASH
        $zip = "$tmp\ffmpeg.zip"
        Invoke-WebRequest 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' -OutFile $zip
        Expand-Archive $zip "$tmp\ffmpeg"
        Get-ChildItem "$tmp\ffmpeg" -Recurse -Include ffmpeg.exe, ffprobe.exe | Copy-Item -Destination dist
        # deno: JavaScript runtime yt-dlp needs for YouTube
        $zip = "$tmp\deno.zip"
        Invoke-WebRequest 'https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip' -OutFile $zip
        Expand-Archive $zip dist -Force
    } finally {
        Remove-Item $tmp -Recurse -Force
    }
}

Copy-Item README.md dist\README.md
Compress-Archive -Path dist\* -DestinationPath dist\mvdl-win64.zip -Force
Write-Host 'done: dist\mvdl-win64.zip'
