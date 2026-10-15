"""Drive the real window on Windows CI (WinForms + WebView2), against a local server:

1. 「URL から探す」 on a page with a <video> -> one video listed
2. add it to the download queue -> saved
3. open a page in the browser tab whose video is loaded by JavaScript -> the request is captured
4. it shows in 「ブラウザで読み込んだ動画」, and can be queued from there -> saved
5. 「ブラウザのページから探す」 -> that video listed (with the browser's cookie)

Run: python tests/smoke_app.py
"""
import http.server
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
SEEN_COOKIES = []


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split('?')[0]
        if p.startswith('/media/'):
            SEEN_COOKIES.append(self.headers.get('Cookie'))
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
result = {'step': 'start', 'errors': []}


def drive(app):
    def fail(msg):
        result['errors'].append(msg)
        app.form.Close()

    app.browser_missing = lambda err: fail(f'WebView2 failed: {err}')
    found_urls = lambda: [c.url for c in app.cands]  # noqa: E731
    steps = [
        ('URL から探す', lambda: (setattr(app.url, 'Text', base + '/video.html'), app.on_find_url()),
         lambda: not app.searching and found_urls() == [base + '/media/plain.mp4']),
        ('ダウンロードに追加', lambda: (setattr(app.outdir, 'Text', outdir), app.found.Items[0].__setattr__('Selected', True),
                                app.on_enqueue()),
         lambda: [j.state for j in app.jobs.values()] == [appmod.DONE]),
        ('ブラウザで開く', lambda: (setattr(app.url, 'Text', base + '/js.html'), app.on_open_in_browser()),
         lambda: app.core is not None and len(app.capture) >= 1),
        ('「ブラウザで読み込んだ動画」タブから追加',
         lambda: (setattr(app.tabs, 'SelectedTab', app.tab_captured), app.captured.Items[0].__setattr__('Selected', True),
                  app.on_enqueue()),
         lambda: app.captured.Items.Count == 1 and app.captured.Items[0].SubItems[3].Text == base + '/media/js.mp4'
         and [j.state for j in app.jobs.values()] == [appmod.DONE, appmod.DONE]),
        ('ブラウザのページから探す', app.on_find_page,
         lambda: not app.searching and found_urls() == [base + '/media/js.mp4']),
    ]
    state = {'i': -1, 'since': time.time()}
    timer = appmod.Timer()
    timer.Interval = 200

    def tick(s, e):
        i = state['i']
        if i >= 0 and not steps[i][2]():
            if time.time() - state['since'] > 60:
                timer.Stop()
                fail(f'timed out at step {i + 1} ({steps[i][0]}): found={found_urls()} '
                     f'jobs={[j.state for j in app.jobs.values()]} captured={len(app.capture)}\n'
                     f'log:\n{app.log_box.Text}')
            return
        if i >= 0:
            print(f'ok: {steps[i][0]}')
        if i + 1 == len(steps):
            timer.Stop()
            result['step'] = 'done'
            result['js_cookie'] = app.cands[0].headers.get('Cookie')
            result['log'] = app.log_box.Text
            app.form.Close()
            return
        state['i'], state['since'] = i + 1, time.time()
        result['step'] = steps[i + 1][0]
        steps[i + 1][1]()

    timer.Tick += tick
    timer.Start()
    app._smoke_timer = timer


appmod.run(setup=drive)
print(result.get('log', ''))
assert not result['errors'], '\n'.join(result['errors'])
assert result['step'] == 'done', result
assert [f for f in os.listdir(outdir) if f.endswith('.mp4')], os.listdir(outdir)
assert result['js_cookie'] and 'sess=abc' in result['js_cookie'], result['js_cookie']
print('app ok')
