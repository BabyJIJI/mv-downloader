"""Open the window, fill the list, close it (run on Windows CI: python tests/smoke_gui.py)."""
import os
import sys
import tempfile
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['APPDATA'] = tempfile.mkdtemp()

from mvdl import gui  # noqa: E402
from mvdl.finder import Candidate  # noqa: E402

gui.SETTINGS = os.path.join(os.environ['APPDATA'], 'mvdl', 'settings.json')
root = tk.Tk()
app = gui.App(root)
app.show([Candidate('https://example.com/a.m3u8', 'テスト', 'HLS', 'ページ内', '720p')])
root.update()
assert app.tree.get_children() == ('0',), app.tree.get_children()
root.after(300, app.on_close)
root.mainloop()
assert os.path.isfile(gui.SETTINGS)
print('gui ok')
