"""Open the window, list videos, queue 3 downloads (2 at a time) from a local server, wait for them,
close. Run on Windows CI: python tests/smoke_gui.py"""
import http.server
import os
import sys
import tempfile
import threading
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['APPDATA'] = tempfile.mkdtemp()

from mvdl import gui  # noqa: E402
from mvdl.finder import Candidate  # noqa: E402

VIDEO = b'\x00\x00\x00\x18ftypmp42' + b'\x00' * 200_000


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        time.sleep(0.5)  # long enough to see two running at once
        self.send_response(200)
        self.send_header('Content-Type', 'video/mp4')
        self.send_header('Content-Length', str(len(VIDEO)))
        self.end_headers()
        if self.command == 'GET':
            self.wfile.write(VIDEO)

    do_HEAD = do_GET


srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f'http://127.0.0.1:{srv.server_port}'
outdir = tempfile.mkdtemp()

gui.SETTINGS = os.path.join(os.environ['APPDATA'], 'mvdl', 'settings.json')
root = tk.Tk()
app = gui.App(root)
app.outdir.set(outdir)
app.parallel.set('2')
app.show([Candidate(f'{base}/v{i}.mp4', f'動画{i}', 'mp4', 'ページ内', {}, '1280×720', 95, 1234567)
          for i in range(3)])
assert app.tree.get_children() == ('0', '1', '2'), app.tree.get_children()
row = app.tree.set('0')
assert (row['resolution'], row['duration'], row['size']) == ('1280×720', '1:35', '1.2 MB'), row

app.tree.selection_set(('0', '1', '2'))
app.on_enqueue()
app.on_enqueue()  # already queued: not added twice
assert len(app.jobs) == 3, len(app.jobs)

max_running, deadline = 0, time.time() + 60
while any(j.state in (gui.WAITING, gui.RUNNING) for j in app.jobs.values()):
    assert time.time() < deadline, [j.state for j in app.jobs.values()]
    root.update()
    max_running = max(max_running, sum(j.state == gui.RUNNING for j in app.jobs.values()))
    time.sleep(0.05)
root.update()
states = [j.state for j in app.jobs.values()]
assert states == [gui.DONE] * 3, states
assert max_running == 2, max_running
assert sorted(os.listdir(outdir)) == ['動画0.mp4', '動画1.mp4', '動画2.mp4'], os.listdir(outdir)

app.on_clear()
assert not app.jobs and not app.jobs_tree.get_children()
root.after(300, app.on_close)
root.mainloop()
assert os.path.isfile(gui.SETTINGS)
print('gui ok')
