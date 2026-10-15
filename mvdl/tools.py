"""Locate helper programs: ffmpeg / deno bundled next to the exe, and a Chromium browser (Edge)."""
import os
import shutil
import sys


def app_dir():
    if getattr(sys, 'frozen', False):  # PyInstaller exe
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_tool(name):
    """Next to the exe, then bin/ beside it, then PATH. `name` is without .exe."""
    exe = name + ('.exe' if os.name == 'nt' else '')
    for d in (app_dir(), os.path.join(app_dir(), 'bin')):
        p = os.path.join(d, exe)
        if os.path.isfile(p):
            return p
    return shutil.which(name)


def find_browser():
    """Edge first (always present on Windows), then Chrome. MVDL_BROWSER overrides."""
    if os.environ.get('MVDL_BROWSER'):
        return os.environ['MVDL_BROWSER']
    if os.name == 'nt':
        roots = [os.environ.get(k) for k in ('ProgramFiles(x86)', 'ProgramFiles', 'LOCALAPPDATA')]
        rels = [r'Microsoft\Edge\Application\msedge.exe', r'Google\Chrome\Application\chrome.exe']
        for rel in rels:
            for root in roots:
                if root and os.path.isfile(os.path.join(root, rel)):
                    return os.path.join(root, rel)
        return None
    for name in ('microsoft-edge', 'google-chrome', 'chromium', 'chromium-browser'):
        if shutil.which(name):
            return shutil.which(name)
    return None


def ydl_base_opts(logger):
    """yt-dlp options shared by search and download."""
    opts = {
        'logger': logger,
        'quiet': True,
        'no_warnings': False,
        'noprogress': True,
        'windowsfilenames': True,
        'socket_timeout': 30,
        'retries': 5,
        'fragment_retries': 10,
    }
    ffmpeg = find_tool('ffmpeg')
    if ffmpeg:
        opts['ffmpeg_location'] = os.path.dirname(ffmpeg)
    deno = find_tool('deno')  # YouTube needs a JS runtime for its signatures
    if deno:
        opts['js_runtimes'] = {'deno': {'path': deno}}
    return opts
