"""For pages that load the player with JavaScript: open the page in headless Edge, start every <video>
muted, and pick the media URLs (mp4 / HLS / DASH manifests) out of the network traffic.

Talks to the browser over the DevTools protocol directly, so no Playwright / browser download is needed.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request

import websocket

from .finder import MEDIA_EXT, Candidate, kind_of
from .tools import find_browser

MEDIA_MIME = ('video/', 'application/vnd.apple.mpegurl', 'application/x-mpegurl', 'audio/mpegurl',
              'application/dash+xml')
SEGMENT_EXT = ('ts', 'm4s', 'aac', 'm4a', 'cmfv', 'cmfa', 'key')

# Runs in every frame: keep trying to start the videos (muted, so autoplay is allowed).
AUTOPLAY_JS = r'''
(() => {
  const kick = () => {
    for (const v of document.querySelectorAll('video')) {
      if (v.paused) { v.muted = true; v.play && v.play().catch(() => {}); }
    }
    for (const b of document.querySelectorAll(
        '.vjs-big-play-button, .ytp-large-play-button, .plyr__control--overlaid, [aria-label*="Play" i], [class*="play-button" i]')) {
      if (!b.dataset.mvdlClicked) { b.dataset.mvdlClicked = '1'; try { b.click(); } catch (e) {} }
    }
  };
  setInterval(kick, 1000);
  document.addEventListener('DOMContentLoaded', kick);
})();
'''


class CDPError(Exception):
    pass


class CDP:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.n = 0
        self.pending = []

    def call(self, method, **params):
        self.n += 1
        self.ws.settimeout(30)
        self.ws.send(json.dumps({'id': self.n, 'method': method, 'params': params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get('id') == self.n:
                if 'error' in msg:
                    raise CDPError(f"{method}: {msg['error'].get('message')}")
                return msg.get('result', {})
            self.pending.append(msg)

    def event(self, timeout):
        if self.pending:
            return self.pending.pop(0)
        self.ws.settimeout(max(timeout, 0.05))
        try:
            return json.loads(self.ws.recv())
        except websocket.WebSocketTimeoutException:
            return None

    def close(self):
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def is_media(url, mime):
    if not url.startswith('http'):
        return False
    path = urllib.parse.urlparse(url).path.lower()
    ext = path.rsplit('.', 1)[-1] if '.' in path.rsplit('/', 1)[-1] else ''
    if ext in SEGMENT_EXT:
        return False
    mime = (mime or '').lower()
    if mime.startswith('video/mp2t'):  # HLS segment
        return False
    return ext in MEDIA_EXT or mime.startswith(MEDIA_MIME)


def _launch(exe, profile):
    args = [exe, '--headless=new', '--remote-debugging-port=0', f'--user-data-dir={profile}',
            '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--mute-audio',
            '--autoplay-policy=no-user-gesture-required', '--window-size=1280,800',
            # keep cross-origin iframes in the page's process so their requests show up too
            '--disable-features=IsolateOrigins,site-per-process', '--disable-site-isolation-trials',
            'about:blank']
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    port_file = os.path.join(profile, 'DevToolsActivePort')
    for _ in range(150):
        if proc.poll() is not None:
            raise RuntimeError('ブラウザが起動しませんでした')
        if os.path.isfile(port_file):
            lines = open(port_file).read().split()
            if lines:
                return proc, int(lines[0])
        time.sleep(0.1)
    proc.kill()
    raise RuntimeError('ブラウザに接続できませんでした')


def sniff(url, log, wait=20, settle=4, stop=None):
    """Return media URLs the page loads within `wait` seconds (stops `settle` s after the last new one)."""
    exe = find_browser()
    if not exe:
        log('Edge / Chrome が見つからないため、ブラウザでの検索はできません')
        return []
    profile = tempfile.mkdtemp(prefix='mvdl-')
    proc = cdp = None
    try:
        proc, port = _launch(exe, profile)
        targets = json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=10))
        page = next(t for t in targets if t.get('type') == 'page')
        cdp = CDP(page['webSocketDebuggerUrl'])
        ua = cdp.call('Browser.getVersion')['userAgent'].replace('HeadlessChrome', 'Chrome')
        cdp.call('Network.enable')
        cdp.call('Network.setUserAgentOverride', userAgent=ua)
        cdp.call('Page.enable')
        cdp.call('Page.addScriptToEvaluateOnNewDocument', source=AUTOPLAY_JS)
        cdp.call('Page.navigate', url=url)

        found = {}  # url -> (mime, referer)
        start = last = time.time()
        while time.time() - start < wait and not (found and time.time() - last > settle):
            if stop is not None and stop.is_set():
                break
            ev = cdp.event(0.5)
            if not ev:
                continue
            m, p = ev.get('method'), ev.get('params', {})
            if m == 'Network.requestWillBeSent':
                u, ref = p['request']['url'], p['request'].get('headers', {}).get('Referer')
                if u not in found and is_media(u, None):
                    found[u], last = ('', ref), time.time()
                    log(f'見つかりました: {u}')
            elif m == 'Network.responseReceived':
                u, mime = p['response']['url'], p['response'].get('mimeType', '')
                if u not in found and is_media(u, mime):
                    found[u], last = (mime, None), time.time()
                    log(f'見つかりました: {u}')

        title = cdp.call('Runtime.evaluate', expression='document.title').get('result', {}).get('value') or url
        page_url = cdp.call('Runtime.evaluate', expression='location.href').get('result', {}).get('value') or url
        out = []
        for u, (_mime, ref) in found.items():
            cookies = cdp.call('Network.getCookies', urls=[u]).get('cookies', [])
            headers = {'Referer': ref or page_url, 'User-Agent': ua}
            if cookies:
                headers['Cookie'] = '; '.join(f"{c['name']}={c['value']}" for c in cookies)
            out.append(Candidate(u, title, kind_of(u) if kind_of(u) != '動画' else 'mp4', 'ブラウザ', '', headers))
        out.sort(key=lambda c: (c.kind not in ('HLS', 'DASH'), 'master' not in c.url.lower()))
        if not out:
            log('ブラウザでも動画は見つかりませんでした')
        return out
    except Exception as e:  # noqa: BLE001
        log(f'ブラウザでの検索に失敗しました: {e}')
        return []
    finally:
        if cdp:
            try:
                cdp.call('Browser.close')
            except Exception:  # noqa: BLE001
                pass
            cdp.close()
        if proc:
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(profile, ignore_errors=True)
