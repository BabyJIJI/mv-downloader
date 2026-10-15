"""For pages that load the player with JavaScript: open the page in headless Edge, start every <video>
muted, and pick the media URLs (mp4 / HLS / DASH manifests) out of the network traffic.

Talks to the browser over the DevTools protocol directly, so no Playwright / browser download is needed.
"""
import json
import os
import subprocess
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


def profile_dir():
    """Kept between runs so a passed Cloudflare check or a login carries over."""
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~/.cache')
    return os.path.join(base, 'mvdl', 'browser')


def _launch(exe, profile, visible):
    args = [exe, '--remote-debugging-port=0', f'--user-data-dir={profile}',
            '--no-first-run', '--no-default-browser-check', '--disable-extensions',
            '--autoplay-policy=no-user-gesture-required', '--window-size=1280,800',
            # keep cross-origin iframes in the page's process so their requests show up too
            '--disable-features=IsolateOrigins,site-per-process', '--disable-site-isolation-trials',
            'about:blank']
    if not visible:
        args[1:1] = ['--headless=new', '--mute-audio']
    port_file = os.path.join(profile, 'DevToolsActivePort')
    try:
        os.remove(port_file)  # left over from the previous run
    except OSError:
        pass
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    for _ in range(150):
        if proc.poll() is not None:
            raise RuntimeError('ブラウザが起動しませんでした（前回のブラウザの画面が開いたままなら閉じてください）')
        if os.path.isfile(port_file):
            lines = open(port_file).read().split()
            if lines:
                return proc, int(lines[0])
        time.sleep(0.1)
    proc.kill()
    raise RuntimeError('ブラウザに接続できませんでした')


def sniff(url, log, wait=20, settle=4, stop=None, visible=False):
    """Return the media URLs the page loads.

    Headless: stops after `wait` s, or `settle` s after the last new one.
    Visible: the user passes Cloudflare / logs in / plays the video; runs until `stop` is set or the
    window is closed. The headers each media request really carried (Cookie, User-Agent, Referer) are
    kept for the download.
    """
    exe = find_browser()
    if not exe:
        log('Edge / Chrome が見つからないため、ブラウザでの検索はできません')
        return []
    profile = profile_dir()
    os.makedirs(profile, exist_ok=True)
    if visible:
        wait = 30 * 60
    proc = cdp = None
    found = {}     # url -> requestId
    sent = {}      # requestId -> headers as sent (from requestWillBeSentExtraInfo)
    page = {'title': url, 'url': url}
    try:
        proc, port = _launch(exe, profile, visible)
        targets = json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/list', timeout=10))
        target = next(t for t in targets if t.get('type') == 'page')
        cdp = CDP(target['webSocketDebuggerUrl'])
        ua = cdp.call('Browser.getVersion')['userAgent'].replace('HeadlessChrome', 'Chrome')
        cdp.call('Network.enable')
        if not visible:
            cdp.call('Network.setUserAgentOverride', userAgent=ua)
        cdp.call('Page.enable')
        cdp.call('Page.addScriptToEvaluateOnNewDocument', source=AUTOPLAY_JS)
        cdp.call('Page.navigate', url=url)
        if visible:
            log('ブラウザの画面で確認（Cloudflare など）やログインを済ませ、動画を再生してください。'
                '終わったら「完了」を押すか、ブラウザを閉じてください')

        start = last = time.time()
        while time.time() - start < wait:
            if stop is not None and stop.is_set():
                break
            if not visible and found and time.time() - last > settle:
                break
            try:
                ev = cdp.event(0.5)
            except (websocket.WebSocketException, OSError):
                log('ブラウザが閉じられました')
                cdp = None
                break
            if not ev:
                continue
            m, p = ev.get('method'), ev.get('params', {})
            if m == 'Network.requestWillBeSent':
                u = p['request']['url']
                if u not in found and is_media(u, None):
                    found[u], last = p['requestId'], time.time()
                    log(f'見つかりました: {u}')
            elif m == 'Network.responseReceived':
                u = p['response']['url']
                if u not in found and is_media(u, p['response'].get('mimeType', '')):
                    found[u], last = p['requestId'], time.time()
                    log(f'見つかりました: {u}')
            elif m == 'Network.requestWillBeSentExtraInfo':
                h = {k.lower(): v for k, v in p.get('headers', {}).items()}
                sent[p['requestId']] = {k: h[k] for k in ('cookie', 'user-agent', 'referer') if k in h}
            elif m == 'Page.loadEventFired':
                try:
                    page['title'] = _eval(cdp, 'document.title') or page['title']
                    page['url'] = _eval(cdp, 'location.href') or page['url']
                except CDPError:
                    pass

        if cdp:
            try:
                page['title'] = _eval(cdp, 'document.title') or page['title']
                page['url'] = _eval(cdp, 'location.href') or page['url']
            except (CDPError, websocket.WebSocketException, OSError):
                pass
        out = []
        for u, rid in found.items():
            h = sent.get(rid, {})
            headers = {'Referer': h.get('referer') or page['url'], 'User-Agent': h.get('user-agent') or ua}
            cookie = h.get('cookie')
            if not cookie and cdp:
                try:
                    cookies = cdp.call('Network.getCookies', urls=[u]).get('cookies', [])
                    cookie = '; '.join(f"{c['name']}={c['value']}" for c in cookies)
                except (CDPError, websocket.WebSocketException, OSError):
                    pass
            if cookie:
                headers['Cookie'] = cookie
            kind = kind_of(u)
            out.append(Candidate(u, page['title'], 'mp4' if kind == '動画' else kind, 'ブラウザ', '', headers))
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


def _eval(cdp, expr):
    return cdp.call('Runtime.evaluate', expression=expr).get('result', {}).get('value')
