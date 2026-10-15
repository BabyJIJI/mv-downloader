"""For pages that load the player with JavaScript: open the page in Chrome (Edge if no Chrome), get the
video going, and pick the media URLs (mp4 / HLS / DASH manifests) out of the network traffic.

Headless (automatic): every <video> is started muted, and the play buttons / players are clicked with real
(trusted) mouse events, since many players only load the video after a click.
Visible: the user passes Cloudflare, logs in and clicks play; we only watch.

The whole browser is watched, not one tab: clicking play often loads the video in a cross-origin player
iframe, a new tab, or a worker. Talks to the browser over the DevTools protocol directly, so no
Playwright / browser download is needed.
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
NETWORK_TARGETS = ('page', 'iframe', 'worker', 'shared_worker', 'service_worker')

# Runs in every frame: keep trying to start the videos (muted, so autoplay is allowed).
AUTOPLAY_JS = r'''
(() => {
  const kick = () => {
    for (const v of document.querySelectorAll('video')) {
      if (v.paused) { v.muted = true; v.play && v.play().catch(() => {}); }
    }
  };
  setInterval(kick, 1000);
  document.addEventListener('DOMContentLoaded', kick);
})();
'''

# The n-th thing worth clicking, scrolled into view: play buttons first, then players / posters /
# thumbnails, larger first. Returns "[x, y]" (viewport coordinates of its center) or null.
PICK_JS = r'''
((n) => {
  const area = e => { const r = e.getBoundingClientRect(); return r.width * r.height; };
  const shown = e => {
    const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width >= 24 && r.height >= 24 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
  };
  const label = e => [e.getAttribute('aria-label'), e.getAttribute('title'), e.id,
                      typeof e.className === 'string' ? e.className : e.className && e.className.baseVal].join(' ');
  const playRe = /(^|[^a-z])play(?!list)|再生/i;
  const buttons = [...document.querySelectorAll('[class*="play" i], [id*="play" i], [aria-label], [title]')]
    .filter(e => playRe.test(label(e)) && shown(e)).sort((a, b) => area(b) - area(a));
  const players = [...document.querySelectorAll(
      'video, iframe, [class*="player" i], [id*="player" i], [class*="poster" i], [class*="thumb" i]')]
    .filter(shown).sort((a, b) => area(b) - area(a));
  const e = [...new Set([...buttons, ...players])][n];
  if (!e) return null;
  e.scrollIntoView({block: 'center', inline: 'center'});
  const r = e.getBoundingClientRect();
  return JSON.stringify([r.left + r.width / 2, r.top + r.height / 2]);
})
'''
CLICK_AT = (2, 5, 9, 14)  # headless: seconds after the page loaded; one more thing clicked each time


class CDPError(Exception):
    pass


class CDP:
    """One browser-level connection; targets are reached through flattened sessions."""

    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30, suppress_origin=True)
        self.n = 0
        self.pending = []

    def call(self, method, session=None, **params):
        self.n += 1
        msg = {'id': self.n, 'method': method, 'params': params}
        if session:
            msg['sessionId'] = session
        self.ws.settimeout(30)
        self.ws.send(json.dumps(msg))
        while True:
            reply = json.loads(self.ws.recv())
            if reply.get('id') == self.n:
                if 'error' in reply:
                    raise CDPError(f"{method}: {reply['error'].get('message')}")
                return reply.get('result', {})
            self.pending.append(reply)

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


def profile_dir(exe):
    """Kept between runs so a passed Cloudflare check or a login carries over. One per browser: a profile
    written by Edge is not meant to be opened by Chrome."""
    base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~/.cache')
    name = os.path.splitext(os.path.basename(exe))[0].lower()
    return os.path.join(base, 'mvdl', f'browser-{name}')


class LaunchError(RuntimeError):
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


EXIT_HANDED_OFF = 21  # Chromium: another browser already uses this profile and took the request


def _launch(exe, profile, visible):
    args = [exe, '--remote-debugging-port=0', f'--user-data-dir={profile}',
            '--no-first-run', '--no-default-browser-check', '--disable-extensions', '--enable-logging=stderr',
            '--autoplay-policy=no-user-gesture-required', '--window-size=1280,800', 'about:blank']
    if not visible:
        args[1:1] = ['--headless=new', '--mute-audio']
    port_file = os.path.join(profile, 'DevToolsActivePort')
    try:
        os.remove(port_file)  # left over from the previous run
    except OSError:
        pass
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    err = tempfile.TemporaryFile()
    proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                            creationflags=flags)
    for _ in range(200):
        if proc.poll() is not None:
            err.seek(0)
            tail = err.read().decode('utf-8', 'replace').strip().splitlines()[-5:]
            raise LaunchError(f'ブラウザが起動しませんでした（終了コード {proc.returncode}）'
                              + ('\n  ' + '\n  '.join(tail) if tail else ''), proc.returncode)
        if os.path.isfile(port_file):
            lines = open(port_file).read().split()
            if lines:
                return proc, int(lines[0])
        time.sleep(0.1)
    proc.kill()
    raise LaunchError('ブラウザに接続できませんでした（20 秒待っても応答がありません）')


def _kill_profile_browsers(profile):
    """End the browser processes left running on our own profile (a window from an earlier run, or one that
    did not shut down). Only processes whose command line names this profile are touched."""
    if os.name != 'nt':
        return
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' or Name='chrome.exe'\" | "
          "Where-Object { $_.CommandLine -like ('*' + $env:MVDL_PROFILE + '*') } | "
          "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], timeout=30,
                   env=os.environ | {'MVDL_PROFILE': profile}, creationflags=subprocess.CREATE_NO_WINDOW,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)


def _start(exe, visible, log):
    """(process, port, profile dir, whether to delete the profile afterwards).

    Headless runs get a fresh profile each time, so a leftover one can never block anything. The visible
    browser keeps its profile (passed checks, logins); if that profile is still in use, the old browser
    is ended and, failing that, a temporary profile is used."""
    if not visible:
        profile = tempfile.mkdtemp(prefix='mvdl-')
        return (*_launch(exe, profile, visible), profile, True)
    profile = profile_dir(exe)
    os.makedirs(profile, exist_ok=True)
    try:
        return (*_launch(exe, profile, visible), profile, False)
    except LaunchError as e:
        log(f'{e}')
    log('前回のブラウザが残っている可能性があるため、終了させてから開き直します')
    try:
        _kill_profile_browsers(profile)
        return (*_launch(exe, profile, visible), profile, False)
    except (LaunchError, OSError, subprocess.SubprocessError) as e:
        log(f'{e}')
    log('一時的なプロファイルで開きます（確認やログインは次回に引き継がれません）')
    profile = tempfile.mkdtemp(prefix='mvdl-')
    return (*_launch(exe, profile, visible), profile, True)


class _Watch:
    """State of one sniff run: the targets we are attached to and the media requests seen."""

    def __init__(self, cdp, url, ua, visible, log):
        self.cdp, self.url, self.ua, self.visible, self.log = cdp, url, ua, visible, log
        self.main = None          # session of our tab
        self.main_target = None
        self.loaded_at = None     # when our tab finished loading (headless clicks are timed from it)
        self.found = {}           # media url -> (session, requestId)
        self.sent = {}            # (session, requestId) -> headers as sent (Cookie / User-Agent / Referer)
        self.last = time.time()   # when the last new media URL showed up
        self.page = {'title': url, 'url': url}
        self.closed = False

    def attach(self, sid, info):
        """New target (tab, popup, cross-origin iframe, worker): watch its network, then let it run."""
        cdp, kind = self.cdp, info.get('type')
        try:
            if kind in NETWORK_TARGETS:
                cdp.call('Network.enable', sid)
                if not self.visible:
                    try:
                        cdp.call('Network.setUserAgentOverride', sid, userAgent=self.ua)
                    except CDPError:
                        pass
            if kind in ('page', 'iframe'):
                cdp.call('Page.enable', sid)
                cdp.call('Page.addScriptToEvaluateOnNewDocument', sid, source=AUTOPLAY_JS)
            cdp.call('Target.setAutoAttach', sid, autoAttach=True, waitForDebuggerOnStart=True, flatten=True)
        except CDPError:
            pass
        finally:
            try:
                cdp.call('Runtime.runIfWaitingForDebugger', sid)
            except CDPError:
                pass
        if info.get('targetId') == self.main_target and not self.main:
            self.main = sid
            cdp.call('Page.navigate', sid, url=self.url)

    def handle(self, ev):
        m, p, sid = ev.get('method'), ev.get('params', {}), ev.get('sessionId')
        if m == 'Target.attachedToTarget':
            self.attach(p['sessionId'], p['targetInfo'])
        elif m == 'Target.detachedFromTarget':
            if p.get('sessionId') == self.main:
                self.closed = True  # the user closed our tab
        elif m == 'Network.requestWillBeSent':
            self.media(p['request']['url'], None, sid, p['requestId'])
        elif m == 'Network.responseReceived':
            self.media(p['response']['url'], p['response'].get('mimeType', ''), sid, p['requestId'])
        elif m == 'Network.requestWillBeSentExtraInfo':
            h = {k.lower(): v for k, v in p.get('headers', {}).items()}
            self.sent[(sid, p['requestId'])] = {k: h[k] for k in ('cookie', 'user-agent', 'referer') if k in h}
        elif m == 'Page.loadEventFired' and sid == self.main:
            self.loaded_at = self.loaded_at or time.time()
            self.read_page()

    def media(self, url, mime, sid, rid):
        if url not in self.found and is_media(url, mime):
            self.found[url], self.last = (sid, rid), time.time()
            self.log(f'見つかりました: {url}')

    def read_page(self):
        try:
            self.page['title'] = _eval(self.cdp, self.main, 'document.title') or self.page['title']
            self.page['url'] = _eval(self.cdp, self.main, 'location.href') or self.page['url']
        except (CDPError, websocket.WebSocketException, OSError):
            pass

    def click(self, n):
        """Click the n-th play button / player with a trusted mouse event."""
        try:
            pt = _eval(self.cdp, self.main, f'{PICK_JS}({n})')
            if not pt:
                return
            x, y = json.loads(pt)
            time.sleep(0.3)  # let the scroll settle
            for kind in ('mouseMoved', 'mousePressed', 'mouseReleased'):
                self.cdp.call('Input.dispatchMouseEvent', self.main, type=kind, x=x, y=y, button='left',
                              clickCount=1)
            self.log('再生ボタン・プレーヤーをクリックしました')
        except (CDPError, ValueError, TypeError):
            pass

    def candidates(self):
        out = []
        for u, key in self.found.items():
            h = self.sent.get(key, {})
            headers = {'Referer': h.get('referer') or self.page['url'], 'User-Agent': h.get('user-agent') or self.ua}
            cookie = h.get('cookie')
            if not cookie and self.main and not self.closed:
                try:
                    cookies = self.cdp.call('Network.getCookies', self.main, urls=[u]).get('cookies', [])
                    cookie = '; '.join(f"{c['name']}={c['value']}" for c in cookies)
                except (CDPError, websocket.WebSocketException, OSError):
                    pass
            if cookie:
                headers['Cookie'] = cookie
            kind = kind_of(u)
            out.append(Candidate(u, self.page['title'], 'mp4' if kind == '動画' else kind, 'ブラウザ', headers))
        return out


def sniff(url, log, wait=25, settle=4, stop=None, visible=False):
    """Return the media URLs the page loads.

    Headless: stops after `wait` s, or `settle` s after the last new one.
    Visible: the user passes Cloudflare / logs in / plays the video; runs until `stop` is set or the
    window is closed. The headers each media request really carried (Cookie, User-Agent, Referer) are
    kept for the download.
    """
    exe = find_browser()
    if not exe:
        log('Chrome（または Edge）が見つからないため、ブラウザでの検索はできません')
        return []
    log(f'使うブラウザ: {exe}')
    if visible:
        wait = 30 * 60
    proc = cdp = None
    profile, temp_profile = None, False
    try:
        proc, port, profile, temp_profile = _start(exe, visible, log)
        version = json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/json/version', timeout=10))
        cdp = CDP(version['webSocketDebuggerUrl'])
        ua = cdp.call('Browser.getVersion')['userAgent'].replace('HeadlessChrome', 'Chrome')
        w = _Watch(cdp, url, ua, visible, log)
        # Our own tab: a visible browser also has hidden pages (side panel, new-tab preload, ...).
        w.main_target = cdp.call('Target.createTarget', url='about:blank')['targetId']
        for t in cdp.call('Target.getTargets').get('targetInfos', []):
            if t['type'] == 'page' and t['targetId'] != w.main_target and t.get('url') == 'about:blank':
                cdp.call('Target.closeTarget', targetId=t['targetId'])  # the empty startup tab
        cdp.call('Target.setAutoAttach', autoAttach=True, waitForDebuggerOnStart=True, flatten=True)
        cdp.call('Target.activateTarget', targetId=w.main_target)
        if visible:
            log('ブラウザの画面で確認（Cloudflare など）やログインを済ませ、動画を再生してください。'
                '終わったら「完了」を押すか、ブラウザを閉じてください')

        start, clicks = time.time(), 0
        while time.time() - start < wait and not w.closed:
            if stop is not None and stop.is_set():
                break
            if not visible and w.found and time.time() - w.last > settle:
                break
            if (not visible and not w.found and w.main and clicks < len(CLICK_AT)
                    and time.time() - (w.loaded_at or start + 5) > CLICK_AT[clicks]):
                w.click(clicks)
                clicks += 1
            try:
                ev = cdp.event(0.5)
            except (websocket.WebSocketException, OSError):
                log('ブラウザが閉じられました')
                w.closed, cdp = True, None
                break
            if ev:
                w.handle(ev)
        if w.closed and cdp:
            log('ブラウザが閉じられました')

        if not w.closed:
            w.read_page()
        out = w.candidates()
        if cdp:  # done with the page: close the window before the (slower) checks
            try:
                cdp.call('Browser.close')
            except Exception:  # noqa: BLE001
                pass
            cdp.close()
            cdp = None
        from .probe import keep_videos
        out = keep_videos(out, log)
        out.sort(key=lambda c: (c.kind not in ('HLS', 'DASH'), 'master' not in c.url.lower()))
        if not out:
            log('ブラウザでも動画は見つかりませんでした'
                + ('' if visible else '。「ブラウザで開いて探す」で、自分で再生してみてください'))
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
        if temp_profile:
            shutil.rmtree(profile, ignore_errors=True)


def _eval(cdp, session, expr):
    return cdp.call('Runtime.evaluate', session, expression=expr, returnByValue=True).get('result', {}).get('value')
