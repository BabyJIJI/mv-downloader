"""Window: paste a URL -> 探す -> pick videos -> ダウンロードに追加 (queued, saved in the background)."""
import itertools
import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .browser import sniff
from .downloader import QUALITIES, Cancelled, download
from .finder import Blocked, Candidate, find_videos, fmt_duration, fmt_size
from .tools import find_tool

SETTINGS = os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~/.config'), 'mvdl', 'settings.json')
PARALLEL = ('1', '2', '3', '4')

WAITING, RUNNING, DONE, FAILED, CANCELLED = '待機中', 'ダウンロード中', '完了', '失敗', '中止'


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


@dataclass
class Job:
    id: str
    cand: Candidate
    outdir: str
    quality: str
    state: str = WAITING
    stop: threading.Event = field(default_factory=threading.Event)
    files: list = field(default_factory=list)


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()     # (kind, payload) from worker threads
        self.stop = threading.Event()  # the current search
        self.searching = False
        self.blocked = False
        self.cands = []
        self.jobs = {}             # id -> Job, in the order they were added
        self.job_ids = (f'j{n}' for n in itertools.count())
        s = load_settings()

        root.title(f'動画ダウンローダー {__version__}')
        root.geometry('960x760')
        root.minsize(700, 560)
        f = ttk.Frame(root, padding=10)
        f.pack(fill='both', expand=True)
        f.columnconfigure(1, weight=1)

        # ---- search
        ttk.Label(f, text='ページの URL').grid(row=0, column=0, sticky='w')
        self.url = tk.StringVar()
        e = ttk.Entry(f, textvariable=self.url)
        e.grid(row=0, column=1, sticky='ew', padx=6)
        e.bind('<Return>', lambda _: self.on_find())
        sf = ttk.Frame(f)
        sf.grid(row=0, column=2, sticky='e')
        self.find_btn = ttk.Button(sf, text='動画を探す', command=self.on_find)
        self.find_btn.pack(side='left')
        self.open_btn = ttk.Button(sf, text='ブラウザで開いて探す', command=self.on_open_browser)
        self.open_btn.pack(side='left', padx=(6, 0))
        self.stop_btn = ttk.Button(sf, text='中止', command=self.on_stop, state='disabled')
        self.stop_btn.pack(side='left', padx=(6, 0))
        self.use_browser = tk.BooleanVar(value=s.get('use_browser', True))
        ttk.Checkbutton(f, text='見つからないときは Chrome で再生して探す（JavaScript で読み込む動画）',
                        variable=self.use_browser).grid(row=1, column=1, columnspan=2, sticky='w', pady=(4, 4))

        self.bar = ttk.Progressbar(f, maximum=1.0, mode='indeterminate')
        self.bar.grid(row=2, column=0, columnspan=3, sticky='ew')
        self.status = tk.StringVar(value='URL を貼り付けて「動画を探す」を押してください')
        ttk.Label(f, textvariable=self.status).grid(row=3, column=0, columnspan=3, sticky='w', pady=(2, 4))

        # ---- found videos
        self.tree = self._table(f, 4, (('title', '見つかった動画', 380, True), ('kind', '種類', 80, False),
                                       ('resolution', '解像度', 95, False), ('duration', '長さ', 65, False),
                                       ('size', 'サイズ', 90, False), ('source', '見つけ方', 70, False)))
        self.tree.bind('<Double-1>', lambda _: self.on_enqueue())
        f.rowconfigure(4, weight=2)

        of = ttk.Frame(f)
        of.grid(row=5, column=0, columnspan=3, sticky='ew', pady=8)
        of.columnconfigure(1, weight=1)
        ttk.Label(of, text='保存先').grid(row=0, column=0)
        self.outdir = tk.StringVar(value=s.get('outdir') or default_outdir())
        ttk.Entry(of, textvariable=self.outdir).grid(row=0, column=1, sticky='ew', padx=6)
        ttk.Button(of, text='参照…', command=self.on_browse).grid(row=0, column=2)
        ttk.Label(of, text='画質').grid(row=0, column=3, padx=(16, 6))
        self.quality = tk.StringVar(value=s.get('quality') if s.get('quality') in QUALITIES else '最高画質')
        ttk.Combobox(of, textvariable=self.quality, values=list(QUALITIES), state='readonly',
                     width=11).grid(row=0, column=4)
        ttk.Button(of, text='ダウンロードに追加 ↓', command=self.on_enqueue).grid(row=0, column=5, padx=(16, 0))

        # ---- download queue
        qf = ttk.Frame(f)
        qf.grid(row=6, column=0, columnspan=3, sticky='ew', pady=(4, 2))
        ttk.Label(qf, text='ダウンロード一覧').pack(side='left')
        self.queue_summary = tk.StringVar()
        ttk.Label(qf, textvariable=self.queue_summary).pack(side='left', padx=10)
        ttk.Button(qf, text='保存先を開く', command=self.on_open).pack(side='right')
        ttk.Button(qf, text='完了・失敗を消す', command=self.on_clear).pack(side='right', padx=6)
        ttk.Button(qf, text='再試行', command=self.on_retry).pack(side='right')
        ttk.Button(qf, text='中止', command=self.on_cancel_jobs).pack(side='right', padx=6)
        self.parallel = tk.StringVar(value=str(s.get('parallel', 2)) if str(s.get('parallel')) in PARALLEL else '2')
        ttk.Combobox(qf, textvariable=self.parallel, values=PARALLEL, state='readonly',
                     width=3).pack(side='right', padx=(0, 10))
        ttk.Label(qf, text='同時に').pack(side='right', padx=(0, 4))
        self.jobs_tree = self._table(f, 7, (('title', 'タイトル', 420, True), ('quality', '画質', 90, False),
                                            ('size', 'サイズ', 90, False), ('state', '状態', 260, False)))
        self.jobs_tree.bind('<Double-1>', lambda _: self.on_open())
        f.rowconfigure(7, weight=2)

        self.log_box = tk.Text(f, height=6, wrap='char', state='disabled', relief='flat',
                               background=root.cget('background'))
        self.log_box.grid(row=8, column=0, columnspan=3, sticky='nsew', pady=(8, 0))
        f.rowconfigure(8, weight=1)

        if not find_tool('ffmpeg'):
            self.log('ffmpeg が見つかりません。exe と同じフォルダに ffmpeg.exe を置くと、高画質の動画も保存できます')
        self._paste_clipboard_url()
        self.update_summary()
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        root.after(100, self.poll)

    # ---- helpers --------------------------------------------------------------------------------
    def _table(self, parent, row, cols):
        tf = ttk.Frame(parent)
        tf.grid(row=row, column=0, columnspan=3, sticky='nsew')
        tf.columnconfigure(0, weight=1)
        tf.rowconfigure(0, weight=1)
        tree = ttk.Treeview(tf, columns=[c[0] for c in cols], show='headings', selectmode='extended', height=6)
        for c, label, w, stretch in cols:
            tree.heading(c, text=label)
            tree.column(c, width=w, stretch=stretch)
        tree.grid(row=0, column=0, sticky='nsew')
        sb = ttk.Scrollbar(tf, orient='vertical', command=tree.yview)
        sb.grid(row=0, column=1, sticky='ns')
        tree.configure(yscrollcommand=sb.set)
        return tree

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

    def set_searching(self, on, stop_label='中止'):
        self.searching = on
        self.find_btn.configure(state='disabled' if on else 'normal')
        self.open_btn.configure(state='disabled' if on else 'normal')
        self.stop_btn.configure(state='normal' if on else 'disabled', text=stop_label if on else '中止')
        if on:
            self.bar.start(15)
        else:
            self.bar.stop()

    def search(self, target, stop_label='中止'):
        self.stop.clear()
        self.set_searching(True, stop_label)

        def work():
            try:
                target()
            except Exception as e:  # noqa: BLE001 - shown to the user
                self.q.put(('error', str(e).replace('ERROR: ', '')))
            finally:
                self.q.put(('search-done', None))

        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                kind, v = self.q.get_nowait()
                if kind == 'log':
                    self.log(v)
                elif kind == 'status':
                    self.status.set(v)
                elif kind == 'found':
                    self.show(v)
                elif kind == 'blocked':
                    self.blocked = True
                elif kind == 'error':
                    self.log('エラー: ' + v)
                    self.status.set('エラーが発生しました（下のログを見てください）')
                elif kind == 'search-done':
                    self.set_searching(False)
                    if self.blocked:  # after 'search-done', so the browser search is not ended by this one
                        self.blocked = False
                        self.ask_browser()
                elif kind == 'job':
                    job_id, text = v
                    self.set_job_state(self.jobs[job_id], text=text)
                elif kind == 'job-end':
                    job_id, state, text = v
                    self.set_job_state(self.jobs[job_id], state, text)
        except queue.Empty:
            pass
        self.start_jobs()
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
            self.tree.insert('', 'end', iid=str(i),
                             values=(label, c.kind, c.resolution or '—', fmt_duration(c.duration),
                                     fmt_size(c.size, c.size_approx), c.source))
        if cands:
            self.tree.selection_set('0')
            self.status.set(f'{len(cands)} 件見つかりました。選んで「ダウンロードに追加」を押してください')
        else:
            self.status.set('動画が見つかりませんでした')

    # ---- download queue -------------------------------------------------------------------------
    def set_job_state(self, job, state=None, text=None):
        if state:
            job.state = state
        if self.jobs_tree.exists(job.id):
            self.jobs_tree.set(job.id, 'state', text or job.state)
        self.update_summary()

    def update_summary(self):
        states = [j.state for j in self.jobs.values()]
        parts = [f'{s} {states.count(s)}' for s in (RUNNING, WAITING, DONE, FAILED) if states.count(s)]
        self.queue_summary.set('（' + '、'.join(parts) + '）' if parts else '')

    def start_jobs(self):
        """Start waiting jobs, oldest first, up to the chosen number at once."""
        running = sum(j.state == RUNNING for j in self.jobs.values())
        for job in self.jobs.values():
            if running >= int(self.parallel.get()):
                break
            if job.state == WAITING:
                self.set_job_state(job, RUNNING, '開始しています…')
                threading.Thread(target=self.run_job, args=(job,), daemon=True).start()
                running += 1

    def run_job(self, job):
        title = job.cand.title
        log = lambda m: self.q.put(('log', f'[{title[:30]}] {m}'))  # noqa: E731

        def progress(frac, text):
            pct = f'{frac * 100:.0f}%  ' if frac is not None else ''
            self.q.put(('job', (job.id, pct + text)))

        try:
            job.files = download(job.cand, job.outdir, job.quality, log, progress, job.stop)
        except Cancelled:
            self.q.put(('job-end', (job.id, CANCELLED, None)))
            return
        except Exception as e:  # noqa: BLE001 - shown in the list and the log
            msg = str(e).replace('ERROR: ', '')
            self.q.put(('log', f'失敗: {title}: {msg}'))
            self.q.put(('job-end', (job.id, FAILED, f'{FAILED}: {msg[:80]}')))
            return
        for p in job.files:
            self.q.put(('log', f'保存しました: {p}'))
        self.q.put(('job-end', (job.id, DONE, None)))

    def selected_jobs(self):
        return [self.jobs[i] for i in self.jobs_tree.selection()]

    # ---- actions --------------------------------------------------------------------------------
    def normalized_url(self):
        url = self.url.get().strip()
        if url and not url.startswith(('http://', 'https://')):
            url = 'https://' + url
            self.url.set(url)
        return url

    def on_find(self):
        url = self.normalized_url()
        if self.searching or not url:
            return
        self.tree.delete(*self.tree.get_children())
        self.log(f'--- {url}')
        self.status.set('探しています…')
        log = lambda m: self.q.put(('log', m))  # noqa: E731

        def work():
            try:
                self.q.put(('found', find_videos(url, log, self.use_browser.get(), self.stop)))
            except Blocked:
                self.q.put(('blocked', None))

        self.search(work)

    def on_open_browser(self):
        """Visible Chrome: the user passes Cloudflare / logs in and plays the video; we watch the traffic."""
        url = self.normalized_url()
        if self.searching or not url:
            return
        self.tree.delete(*self.tree.get_children())
        self.log(f'--- {url}（ブラウザ）')
        self.status.set('ブラウザで確認を済ませて動画を再生し、終わったら「完了」を押してください')
        log = lambda m: self.q.put(('log', m))  # noqa: E731
        self.search(lambda: self.q.put(('found', sniff(url, log, stop=self.stop, visible=True))), '完了')

    def on_stop(self):
        self.stop.set()
        self.status.set('ブラウザを閉じています…' if self.stop_btn.cget('text') == '完了' else '中止しています…')

    def on_enqueue(self):
        sel = [self.cands[int(i)] for i in self.tree.selection()]
        if not sel:
            messagebox.showinfo('動画ダウンローダー', 'ダウンロードする動画を上の一覧から選んでください')
            return
        outdir, quality = self.outdir.get().strip() or default_outdir(), self.quality.get()
        self.save()
        active = {(j.cand.url, j.quality) for j in self.jobs.values() if j.state in (WAITING, RUNNING)}
        added = 0
        for c in sel:
            if (c.url, quality) in active:
                self.log(f'追加済みです: {c.title}')
                continue
            job = Job(next(self.job_ids), c, outdir, quality)
            self.jobs[job.id] = job
            self.jobs_tree.insert('', 'end', iid=job.id,
                                  values=(c.title, quality, fmt_size(c.size, c.size_approx), WAITING))
            added += 1
        if added:
            self.status.set(f'{added} 件をダウンロード一覧に追加しました')
        self.update_summary()
        self.start_jobs()

    def on_cancel_jobs(self):
        jobs = self.selected_jobs() or [j for j in self.jobs.values() if j.state in (WAITING, RUNNING)]
        for job in jobs:
            if job.state == WAITING:
                self.set_job_state(job, CANCELLED)
            elif job.state == RUNNING:
                job.stop.set()
                self.set_job_state(job, text='中止しています…')

    def on_retry(self):
        for job in self.selected_jobs():
            if job.state in (FAILED, CANCELLED):
                job.stop = threading.Event()
                self.set_job_state(job, WAITING)
        self.start_jobs()

    def on_clear(self):
        for job in list(self.jobs.values()):
            if job.state in (DONE, FAILED, CANCELLED):
                del self.jobs[job.id]
                self.jobs_tree.delete(job.id)
        self.update_summary()

    def on_browse(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get() or default_outdir())
        if d:
            self.outdir.set(os.path.normpath(d))

    def on_open(self):
        jobs = self.selected_jobs()
        d = jobs[0].outdir if jobs else (self.outdir.get().strip() or default_outdir())
        os.makedirs(d, exist_ok=True)
        open_folder(d)

    def save(self):
        save_settings({'outdir': self.outdir.get().strip(), 'quality': self.quality.get(),
                       'use_browser': self.use_browser.get(), 'parallel': int(self.parallel.get())})

    def on_close(self):
        left = sum(j.state in (WAITING, RUNNING) for j in self.jobs.values())
        if left and not messagebox.askyesno('動画ダウンローダー',
                                            f'ダウンロードが {left} 件残っています。中止して終了しますか？'):
            return
        self.stop.set()
        for job in self.jobs.values():
            job.stop.set()
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
