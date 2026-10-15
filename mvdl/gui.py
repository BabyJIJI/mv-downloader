"""Window: paste a URL -> 探す -> pick videos -> ダウンロード."""
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .downloader import QUALITIES, Cancelled, download
from .browser import sniff
from .finder import Blocked, find_videos
from .tools import find_tool

SETTINGS = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~/.config'), 'mvdl', 'settings.json')


def load_settings():
    try:
        with open(SETTINGS, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(s):
    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS, 'w', encoding='utf-8') as f:
            json.dump(s, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def default_outdir():
    return os.path.join(os.path.expanduser('~'), 'Downloads')


def open_folder(path):
    if os.name == 'nt':
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(['xdg-open', path])


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()     # (kind, payload) from worker threads
        self.stop = threading.Event()
        self.busy = False
        self.blocked = False
        self.cands = []
        s = load_settings()

        root.title(f'動画ダウンローダー {__version__}')
        root.geometry('900x620')
        root.minsize(640, 460)
        f = ttk.Frame(root, padding=10)
        f.pack(fill='both', expand=True)
        f.columnconfigure(1, weight=1)
        f.columnconfigure(3, weight=0)

        ttk.Label(f, text='ページの URL').grid(row=0, column=0, sticky='w')
        self.url = tk.StringVar()
        e = ttk.Entry(f, textvariable=self.url)
        e.grid(row=0, column=1, sticky='ew', padx=6)
        e.bind('<Return>', lambda _: self.on_find())
        self.find_btn = ttk.Button(f, text='動画を探す', command=self.on_find)
        self.find_btn.grid(row=0, column=2)
        self.open_btn = ttk.Button(f, text='ブラウザで開いて探す', command=self.on_open_browser)
        self.open_btn.grid(row=0, column=3, padx=(6, 0))
        self.use_browser = tk.BooleanVar(value=s.get('use_browser', True))
        ttk.Checkbutton(f, text='見つからないときは Edge で再生して探す（JavaScript で読み込む動画）',
                        variable=self.use_browser).grid(row=1, column=1, columnspan=3, sticky='w', pady=(4, 6))

        cols = ('title', 'kind', 'detail', 'source')
        tf = ttk.Frame(f)
        tf.grid(row=2, column=0, columnspan=4, sticky='nsew')
        f.rowconfigure(2, weight=3)
        tf.columnconfigure(0, weight=1)
        tf.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(tf, columns=cols, show='headings', selectmode='extended')
        for c, label, w, stretch in (('title', 'タイトル / URL', 460, True), ('kind', '種類', 110, False),
                                     ('detail', '画質・長さ', 120, False), ('source', '見つけ方', 80, False)):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, stretch=stretch)
        self.tree.grid(row=0, column=0, sticky='nsew')
        sb = ttk.Scrollbar(tf, orient='vertical', command=self.tree.yview)
        sb.grid(row=0, column=1, sticky='ns')
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind('<Double-1>', lambda _: self.on_download())

        ttk.Label(f, text='保存先').grid(row=3, column=0, sticky='w', pady=(8, 0))
        self.outdir = tk.StringVar(value=s.get('outdir') or default_outdir())
        ttk.Entry(f, textvariable=self.outdir).grid(row=3, column=1, sticky='ew', padx=6, pady=(8, 0))
        ttk.Button(f, text='参照…', command=self.on_browse).grid(row=3, column=2, columnspan=2, sticky='w', pady=(8, 0))

        bf = ttk.Frame(f)
        bf.grid(row=4, column=0, columnspan=4, sticky='ew', pady=8)
        ttk.Label(bf, text='画質').pack(side='left')
        self.quality = tk.StringVar(value=s.get('quality') if s.get('quality') in QUALITIES else '最高画質')
        ttk.Combobox(bf, textvariable=self.quality, values=list(QUALITIES), state='readonly',
                     width=12).pack(side='left', padx=(6, 16))
        self.dl_btn = ttk.Button(bf, text='選んだ動画をダウンロード', command=self.on_download)
        self.dl_btn.pack(side='left')
        self.stop_btn = ttk.Button(bf, text='中止', command=self.on_stop, state='disabled')
        self.stop_btn.pack(side='left', padx=6)
        ttk.Button(bf, text='保存先を開く', command=self.on_open).pack(side='right')

        self.bar = ttk.Progressbar(f, maximum=1.0)
        self.bar.grid(row=5, column=0, columnspan=4, sticky='ew')
        self.status = tk.StringVar(value='URL を貼り付けて「動画を探す」を押してください')
        ttk.Label(f, textvariable=self.status).grid(row=6, column=0, columnspan=4, sticky='w', pady=(4, 4))

        self.log_box = tk.Text(f, height=8, wrap='char', state='disabled', relief='flat',
                               background=root.cget('background'))
        self.log_box.grid(row=7, column=0, columnspan=4, sticky='nsew')
        f.rowconfigure(7, weight=1)

        if not find_tool('ffmpeg'):
            self.log('ffmpeg が見つかりません。exe と同じフォルダに ffmpeg.exe を置くと、高画質の動画も保存できます')
        self._paste_clipboard_url()
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        root.after(100, self.poll)

    # ---- helpers --------------------------------------------------------------------------------
    def _paste_clipboard_url(self):
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            return
        if text.startswith(('http://', 'https://')) and '\n' not in text:
            self.url.set(text)

    def log(self, msg):
        self.log_box.configure(state='normal')
        self.log_box.insert('end', msg + '\n')
        self.log_box.see('end')
        self.log_box.configure(state='disabled')

    def set_busy(self, busy):
        self.busy = busy
        self.find_btn.configure(state='disabled' if busy else 'normal')
        self.open_btn.configure(state='disabled' if busy else 'normal')
        self.dl_btn.configure(state='disabled' if busy else 'normal')
        self.stop_btn.configure(state='normal' if busy else 'disabled')
        if not busy:
            self.stop_btn.configure(text='中止')
            self.bar.stop()
            self.bar.configure(mode='determinate')

    def run(self, target, *args):
        self.stop.clear()
        self.set_busy(True)

        def work():
            try:
                target(*args)
            except Exception as e:  # noqa: BLE001 - shown to the user
                self.q.put(('error', str(e).replace('ERROR: ', '')))
            finally:
                self.q.put(('done', None))

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, v = self.q.get_nowait()
                if kind == 'log':
                    self.log(v)
                elif kind == 'status':
                    self.status.set(v)
                elif kind == 'progress':
                    frac, text = v
                    if frac is None:
                        if str(self.bar.cget('mode')) != 'indeterminate':
                            self.bar.configure(mode='indeterminate')
                            self.bar.start(15)
                    else:
                        if str(self.bar.cget('mode')) != 'determinate':
                            self.bar.stop()
                            self.bar.configure(mode='determinate')
                        self.bar['value'] = frac
                    self.status.set(text)
                elif kind == 'found':
                    self.show(v)
                elif kind == 'blocked':
                    self.blocked = True
                elif kind == 'error':
                    self.log('エラー: ' + v)
                    self.status.set('エラーが発生しました（下のログを見てください）')
                elif kind == 'done':
                    self.set_busy(False)
                    if self.blocked:  # after 'done', so the browser run is not marked finished by this one
                        self.blocked = False
                        self.ask_browser()
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def ask_browser(self):
        if messagebox.askyesno(
                '動画ダウンローダー',
                'Cloudflare の確認画面（ボット対策）で止められました。\n\n'
                'ブラウザを開くので、確認を通して動画を再生してください。'
                '見つかった動画を一覧に出します。\n\nブラウザを開きますか？'):
            self.on_open_browser()
        else:
            self.status.set('Cloudflare の確認画面で止められました')

    def show(self, cands):
        self.cands = cands
        self.tree.delete(*self.tree.get_children())
        for i, c in enumerate(cands):
            label = c.title if c.source == 'yt-dlp' else f'{c.title}  —  {c.url}'
            self.tree.insert('', 'end', iid=str(i), values=(label, c.kind, c.detail, c.source))
        if cands:
            self.tree.selection_set('0')
            self.status.set(f'{len(cands)} 件見つかりました。選んで「ダウンロード」を押してください')
        else:
            self.status.set('動画が見つかりませんでした')

    # ---- actions --------------------------------------------------------------------------------
    def on_find(self):
        url = self.normalized_url()
        if self.busy or not url:
            return
        self.tree.delete(*self.tree.get_children())
        self.log(f'--- {url}')
        self.q.put(('progress', (None, '探しています…')))
        log = lambda m: self.q.put(('log', m))  # noqa: E731

        def work():
            try:
                self.q.put(('found', find_videos(url, log, self.use_browser.get(), self.stop)))
            except Blocked:
                self.q.put(('blocked', None))

        self.run(work)

    def on_open_browser(self):
        """Visible Edge: the user passes Cloudflare / logs in and plays the video; we watch the traffic."""
        url = self.normalized_url()
        if self.busy or not url:
            return
        self.tree.delete(*self.tree.get_children())
        self.log(f'--- {url}（ブラウザ）')
        self.q.put(('progress', (None, 'ブラウザで確認を済ませて動画を再生し、終わったら「完了」を押してください')))
        log = lambda m: self.q.put(('log', m))  # noqa: E731
        self.run(lambda: self.q.put(('found', sniff(url, log, stop=self.stop, visible=True))))
        self.stop_btn.configure(text='完了')

    def normalized_url(self):
        url = self.url.get().strip()
        if url and not url.startswith(('http://', 'https://')):
            url = 'https://' + url
            self.url.set(url)
        return url

    def on_download(self):
        if self.busy:
            return
        sel = [self.cands[int(i)] for i in self.tree.selection()]
        if not sel:
            messagebox.showinfo('動画ダウンローダー', 'ダウンロードする動画を一覧から選んでください')
            return
        outdir, quality = self.outdir.get().strip() or default_outdir(), self.quality.get()
        self.save()
        log = lambda m: self.q.put(('log', m))  # noqa: E731

        def work():
            ok = 0
            for n, c in enumerate(sel, 1):
                if self.stop.is_set():
                    break
                head = f'[{n}/{len(sel)}] ' if len(sel) > 1 else ''
                self.q.put(('progress', (0.0, f'{head}{c.title}')))
                try:
                    files = download(c, outdir, quality, log,
                                     lambda frac, text: self.q.put(('progress', (frac, head + text))), self.stop)
                except Cancelled:
                    log('中止しました')
                    break
                except Exception as e:  # noqa: BLE001 - keep going with the rest
                    log(f'失敗: {c.title}: {str(e).replace("ERROR: ", "")}')
                    continue
                ok += 1
                for p in files:
                    log(f'保存しました: {p}')
            self.q.put(('progress', (1.0 if ok else 0.0, f'{ok} / {len(sel)} 件を保存しました')))

        self.run(work)

    def on_stop(self):
        self.stop.set()
        self.status.set('ブラウザを閉じています…' if self.stop_btn.cget('text') == '完了' else '中止しています…')

    def on_browse(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get() or default_outdir())
        if d:
            self.outdir.set(os.path.normpath(d))

    def on_open(self):
        d = self.outdir.get().strip() or default_outdir()
        os.makedirs(d, exist_ok=True)
        open_folder(d)

    def save(self):
        save_settings({'outdir': self.outdir.get().strip(), 'quality': self.quality.get(),
                       'use_browser': self.use_browser.get()})

    def on_close(self):
        self.stop.set()
        self.save()
        self.root.destroy()


def main():
    if os.name == 'nt':
        try:  # sharp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    root = tk.Tk()
    if os.name == 'nt':
        import tkinter.font as tkfont
        for name in ('TkDefaultFont', 'TkTextFont', 'TkHeadingFont', 'TkMenuFont'):
            tkfont.nametofont(name).configure(family='Yu Gothic UI', size=10)
        tkfont.nametofont('TkFixedFont').configure(family='BIZ UDGothic', size=9)
    ttk.Style(root).theme_use('vista' if os.name == 'nt' else 'clam')
    App(root)
    root.mainloop()


if __name__ == '__main__':
    sys.exit(main())
