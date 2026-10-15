"""Drive the real window on Windows CI (the web-page interface + the built-in WebView2 browser), against a
local server. Commands go in the way the page sends them (App.handle), and at the end the page itself is
asked what it shows.

1. 「URL から探す」 on a page with a <video> -> one video listed
2. add it to the download queue -> saved
3. open a page in the browser whose video is loaded by JavaScript -> the request is captured, the browser
   is laid over the 「ブラウザ」 tab
4. queue it from 「ブラウザで読み込んだ動画」 -> saved
5. 「ブラウザのページから探す」 -> that video listed (with the browser's cookie)

Run: python tests/smoke_app.py
"""
import http.server
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8')  # the CI console is cp1252; the log is Japanese
for var in ('APPDATA', 'LOCALAPPDATA'):  # settings and the browser profile go to a scratch place
    os.environ[var] = tempfile.mkdtemp()

import mvdl.probe  # noqa: E402

mvdl.probe.find_tool = lambda name: None  # placeholder bytes below: judge by Content-Type
from mvdl import app as appmod  # noqa: E402

VIDEO = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 100_000
PAGES = {
    '/video.html': '<title>Video page</title><video src="/media/plain.mp4"></video>',
    '/js.html': '<title>JS page</title><div id=p></div><script>document.cookie = "sess=abc; path=/";'
                'setTimeout(() => { const v = document.createElement("video"); v.muted = true; v.autoplay = true;'
                ' v.src = "/media/" + "js.mp4"; document.getElementById("p").appendChild(v); }, 1000);</script>',
}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split('?')[0]
        if p.startswith('/media/'):
            data, ctype = VIDEO, 'video/mp4'
        elif p in PAGES:
            data, ctype = PAGES[p].encode(), 'text/html; charset=utf-8'
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_HEAD = do_GET


srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{srv.server_port}'
outdir = tempfile.mkdtemp()
SHOTS = os.environ.get('MVDL_SHOTS') or os.path.join(tempfile.gettempdir(), 'mvdl-shots')
result = {'step': 'start', 'errors': []}
logs = []


def drive(app):
    send = app.send

    def capture_send(type_, **data):  # keep the log lines for the report
        if type_ == 'log':
            logs.append(data['text'])
        send(type_, **data)

    app.send = capture_send

    def fail(msg):
        result['errors'].append(msg)
        app.form.Close()

    app.browser_missing = lambda err: fail(f'WebView2 failed: {err}')
    found = lambda: [c.url for c in app.cands]  # noqa: E731
    states = lambda: [j.state for j in app.jobs.values()]  # noqa: E731
    view = {}

    shots = {}

    def screenshot(name):
        """The interface page as an image (CI uploads them, to look at the design)."""
        from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat
        from System.IO import FileMode, FileStream
        os.makedirs(SHOTS, exist_ok=True)
        stream = FileStream(os.path.join(SHOTS, f'{name}.png'), FileMode.Create)
        task = app.ui_core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream)
        app.when_done([task], lambda: (stream.Close(), shots.__setitem__(name, True)))

    def ask_page():
        task = app.ui_core.ExecuteScriptAsync('JSON.stringify(mvdlView())')
        app.when_done([task], lambda: view.update(json.loads(json.loads(task.Result))))

    steps = [
        ('画面とブラウザの準備', lambda: None, lambda: app.ui_ready and app.core is not None),
        ('URL から探す', lambda: app.handle({'cmd': 'set', 'key': 'outdir', 'value': outdir})
         or app.handle({'cmd': 'findUrl', 'url': base + '/video.html'}),
         lambda: not app.searching and found() == [base + '/media/plain.mp4']),
        ('ダウンロードに追加', lambda: app.handle({'cmd': 'enqueue', 'from': 'found', 'ids': ['0']}),
         lambda: states() == [appmod.DONE]),
        ('ブラウザで開く', lambda: app.handle({'cmd': 'openInBrowser', 'url': base + '/js.html'}),
         lambda: len(app.capture) >= 1 and app.web.Visible and app.web.Width > 100
         and getattr(app.checked.get(base + '/media/js.mp4'), 'size', None) == len(VIDEO)),
        ('「ブラウザで読み込んだ動画」から追加',
         lambda: app.handle({'cmd': 'enqueue', 'from': 'captured', 'ids': [base + '/media/js.mp4']}),
         lambda: states() == [appmod.DONE, appmod.DONE]),
        ('ブラウザのページから探す', lambda: app.handle({'cmd': 'findPage'}),
         lambda: not app.searching and found() == [base + '/media/js.mp4']),
        ('画面の表示', ask_page, lambda: bool(view)),
        ('スクリーンショット（見つかった動画）', lambda: screenshot('found'), lambda: 'found' in shots),
        ('スクリーンショット（ダウンロード一覧）', lambda: app.send('tab', name='queue'), lambda: True),
        ('', lambda: screenshot('queue'), lambda: 'queue' in shots),
        ('スクリーンショット（ブラウザで読み込んだ動画）', lambda: app.send('tab', name='captured'), lambda: True),
        ('', lambda: screenshot('captured'), lambda: 'captured' in shots),
    ]
    state = {'i': -1, 'since': time.time()}
    timer = appmod.Timer()
    timer.Interval = 200

    def tick(s, e):
        i = state['i']
        if i >= 0 and not steps[i][2]():
            if time.time() - state['since'] > 60:
                timer.Stop()
                fail(f'timed out at step {i + 1} ({steps[i][0]}): found={found()} jobs={states()} '
                     f'captured={len(app.capture)} web.Visible={app.web.Visible}')
            return
        if i >= 0 and steps[i][0]:
            print(f'ok: {steps[i][0]}')
        if i + 1 == len(steps):
            timer.Stop()
            result.update(step='done', view=view, js_cookie=app.cands[0].headers.get('Cookie'))
            app.form.Close()
            return
        state['i'], state['since'] = i + 1, time.time()
        result['step'] = steps[i + 1][0]
        steps[i + 1][1]()

    timer.Tick += tick
    timer.Start()
    app._smoke_timer = timer


appmod.run(setup=drive)
print('\n'.join(logs))
assert not result['errors'], '\n'.join(result['errors'])
assert result['step'] == 'done', result
assert len([f for f in os.listdir(outdir) if f.endswith('.mp4')]) == 2, os.listdir(outdir)
assert result['js_cookie'] and 'sess=abc' in result['js_cookie'], result['js_cookie']
assert result['view'] == {'tab': 'found', 'found': 1, 'captured': 1, 'queue': 2}, result['view']
print('app ok')
