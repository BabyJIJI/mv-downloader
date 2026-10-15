"""Drive the real browser on Windows CI: find Chrome, then catch a JavaScript-loaded video and one that
only loads after a real click (headless), and catch a video in a visible window (stopped like the
「完了」 button). Run: python tests/smoke_browser.py"""
import http.server
import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mvdl.probe  # noqa: E402
from mvdl.browser import sniff  # noqa: E402
from mvdl.tools import find_browser  # noqa: E402

mvdl.probe.find_tool = lambda name: None  # placeholder bytes below: judge by Content-Type
VIDEO = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 4096
PAGES = {
    '/js.html': '<title>JS</title><div id=p></div><script>setTimeout(() => { const v = document.createElement("video");'
                ' v.src = "/media/" + "js.mp4"; document.getElementById("p").appendChild(v); }, 1000);</script>',
    '/click.html': '<title>Click</title><div id=cover class="player-cover" style="width:640px;height:360px;'
                   'background:#333"></div><script>cover.addEventListener("click", e => { if (!e.isTrusted) return;'
                   ' const v = document.createElement("video"); v.src = "/media/" + "click.mp4";'
                   ' document.body.appendChild(v); });</script>',
}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        p = self.path.split('?')[0]
        data, ctype = (VIDEO, 'video/mp4') if p.startswith('/media/') else (PAGES[p].encode(), 'text/html')
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)


srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{srv.server_port}'

exe = find_browser()
print('browser:', exe)
assert exe and os.path.basename(exe).lower() == 'chrome.exe', exe

for page, name in (('js.html', 'js.mp4'), ('click.html', 'click.mp4')):
    found = sniff(f'{base}/{page}', print)
    assert [c.url for c in found] == [f'{base}/media/{name}'], (page, found)

stop = threading.Event()
threading.Timer(12, stop.set).start()  # the user pressing 「完了」
found = sniff(f'{base}/js.html', print, stop=stop, visible=True)
assert [c.url for c in found] == [f'{base}/media/js.mp4'], found
print('browser ok')
