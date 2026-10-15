"""The window. The interface is a web page (ui/index.html) in one WebView2; the built-in browser is a second
WebView2 laid over the page's 「ブラウザ」 tab. They talk over postMessage: the page sends commands
({cmd: ...}), this side sends updates ({type: ...}).

「URL から探す」 looks at a URL with yt-dlp and the page's HTML. The built-in browser is for pages that need
a person: pass Cloudflare, log in, play the video, then 「ブラウザのページから探す」. While browsing, the
browser only reports what it loaded (capture.py); the page is not touched until the button is pressed.
WinForms + WebView2 through pythonnet; Windows only.
"""
import itertools
import json
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field

import clr  # pythonnet

from . import __version__
from .capture import Capture, is_media
from .downloader import QUALITIES, Cancelled, download
from .finder import Blocked, Candidate, find_in_page, find_videos, fmt_duration, fmt_size, kind_of
from .tools import find_tool

clr.AddReference('System.Windows.Forms')
clr.AddReference('System.Drawing')
HERE = os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'mvdl')
LIB, UI = os.path.join(HERE, 'lib'), os.path.join(HERE, 'ui')
clr.AddReference(os.path.join(LIB, 'Microsoft.Web.WebView2.Core.dll'))
clr.AddReference(os.path.join(LIB, 'Microsoft.Web.WebView2.WinForms.dll'))

from Microsoft.Web.WebView2.Core import CoreWebView2Environment, CoreWebView2HostResourceAccessKind  # noqa: E402
from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2  # noqa: E402
from System.Drawing import Color, Size  # noqa: E402
from System.Threading import ApartmentState, Thread, ThreadStart  # noqa: E402
from System.Windows.Forms import (  # noqa: E402
    Application, Clipboard, DialogResult, DockStyle, FolderBrowserDialog, Form, MessageBox, MessageBoxButtons,
    MessageBoxIcon, Timer)

APP = '動画ダウンローダー'
DATA = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'mvdl')
SETTINGS = os.path.join(os.environ.get('APPDATA') or DATA, 'mvdl', 'settings.json')
WEBVIEW2_RUNTIME = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
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


def normalized(url):
    url = (url or '').strip()
    if url and '://' not in url and not url.startswith('about:'):
        url = 'https://' + url
    return url


def dark_mode():
    """Windows' app theme (Settings > Personalization > Colors)."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as k:
            return winreg.QueryValueEx(k, 'AppsUseLightTheme')[0] == 0
    except OSError:
        return False


@dataclass
class Job:
    id: str
    cand: Candidate
    outdir: str
    quality: str
    state: str = WAITING
    text: str = ''
    progress: float | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    files: list = field(default_factory=list)


class App:
    def __init__(self, start_url=None):
        self.q = queue.Queue()        # (kind, payload) from worker threads, drained by a UI timer
        self.capture = Capture()
        self.searching = False
        self.cands = []
        self.jobs = {}                # id -> Job, in the order they were added
        self.job_ids = (f'j{n}' for n in itertools.count())
        self.timers = set()           # keep task-wait timers alive
        self.ui_core = None           # CoreWebView2 of the interface page
        self.core = None              # CoreWebView2 of the built-in browser
        self.ui_ready = False         # the page said 'ready': messages can go out
        self.outbox = []              # messages waiting for the page
        self.start_url = start_url
        self.settings = s = load_settings()
        self.outdir = s.get('outdir') or default_outdir()
        self.quality = s.get('quality') if s.get('quality') in QUALITIES else '最高画質'
        self.parallel = int(s.get('parallel', 2)) if str(s.get('parallel')) in PARALLEL else 2
        self.dark = dark_mode()

        f = self.form = Form()
        f.Text = APP
        f.Size = Size(1500, 940)
        f.MinimumSize = Size(1100, 680)
        f.BackColor = Color.FromArgb(31, 31, 31) if self.dark else Color.FromArgb(243, 243, 243)

        props = CoreWebView2CreationProperties()
        props.UserDataFolder = os.path.join(DATA, 'webview2')  # cookies / logins / passed checks persist
        self.ui = WebView2()
        self.ui.Dock = DockStyle.Fill
        self.ui.DefaultBackgroundColor = f.BackColor
        self.ui.CreationProperties = props
        self.ui.CoreWebView2InitializationCompleted += self.on_ui_ready
        self.web = WebView2()
        self.web.Visible = False
        self.web.CreationProperties = props
        self.web.CoreWebView2InitializationCompleted += self.on_web_ready
        f.Controls.Add(self.web)
        f.Controls.Add(self.ui)
        self.web.BringToFront()

        f.Load += lambda s, e: self.on_load()
        f.HandleCreated += lambda s, e: self.dark_title_bar()
        f.FormClosing += self.on_closing
        self.ticker = Timer()
        self.ticker.Interval = 100
        self.ticker.Tick += lambda s, e: self.poll()
        self.ticker.Start()

    # ---- start ------------------------------------------------------------------------------------
    def on_load(self):
        try:
            CoreWebView2Environment.SetLoaderDllFolderPath(LIB)
            self.ui.EnsureCoreWebView2Async(None)
            self.web.EnsureCoreWebView2Async(None)
        except Exception as e:  # noqa: BLE001
            self.browser_missing(e)

    def dark_title_bar(self):
        if not self.dark:
            return
        try:
            import ctypes
            value = ctypes.c_int(1)  # DWMWA_USE_IMMERSIVE_DARK_MODE = 20
            ctypes.windll.dwmapi.DwmSetWindowAttribute(int(str(self.form.Handle)), 20, ctypes.byref(value), 4)
        except Exception:  # noqa: BLE001 - only cosmetic
            pass

    def browser_missing(self, err):
        MessageBox.Show(f'ブラウザの部品（Microsoft Edge WebView2 ランタイム）を使えませんでした。\n\n'
                        f'{err}\n\nWebView2 ランタイムをインストールしてから起動し直してください:\n{WEBVIEW2_RUNTIME}',
                        APP, MessageBoxButtons.OK, MessageBoxIcon.Error)
        self.form.Close()

    def on_ui_ready(self, sender, e):
        if not e.IsSuccess:
            self.browser_missing(e.InitializationException)
            return
        core = self.ui_core = self.ui.CoreWebView2
        st = core.Settings
        st.AreDevToolsEnabled = bool(os.environ.get('MVDL_DEVTOOLS'))
        st.AreDefaultContextMenusEnabled = False
        st.IsZoomControlEnabled = False
        st.IsStatusBarEnabled = False
        st.AreBrowserAcceleratorKeysEnabled = False
        core.SetVirtualHostNameToFolderMapping('mvdl.ui', UI, CoreWebView2HostResourceAccessKind.Allow)
        core.WebMessageReceived += self.on_message
        core.Navigate('https://mvdl.ui/index.html')

    def on_web_ready(self, sender, e):
        if not e.IsSuccess:
            self.send('browserFailed', text=f'ブラウザを使えません: {e.InitializationException}')
            return
        core = self.core = self.web.CoreWebView2
        core.WebResourceResponseReceived += self.on_response
        core.NavigationStarting += self.on_navigation
        core.SourceChanged += lambda s, a: self.send('browser', url=core.Source)
        core.NewWindowRequested += self.on_new_window
        self.log(f'ブラウザ: WebView2 {core.Environment.BrowserVersionString}')
        if os.environ.get('MVDL_STARTUP_CHECK'):  # build check: the exe starts and its browser comes up
            with open(os.environ['MVDL_STARTUP_CHECK'], 'w', encoding='utf-8') as fh:
                fh.write(core.Environment.BrowserVersionString)
            self.form.Close()
            return
        last = self.settings.get('last_url')
        if last:
            core.Navigate(last)

    # ---- talking to the page ------------------------------------------------------------------------
    def send(self, type_, **data):
        msg = json.dumps({'type': type_, **data}, ensure_ascii=False)
        if self.ui_ready:
            self.ui_core.PostWebMessageAsJson(msg)
        else:
            self.outbox.append(msg)

    def on_message(self, sender, e):
        try:
            self.handle(json.loads(e.WebMessageAsJson))
        except Exception as err:  # noqa: BLE001 - never let one command take the window down
            self.log(f'エラー: {err}')

    def handle(self, m):
        """A command from the page (also used by the tests)."""
        cmd = m.get('cmd')
        if cmd == 'ready':
            self.ui_ready = True
            self.send('init', version=__version__, qualities=list(QUALITIES), parallel=list(PARALLEL),
                      settings={'outdir': self.outdir, 'quality': self.quality, 'parallel': self.parallel},
                      url=self.start_url or self.clipboard_url() or '', ffmpeg=bool(find_tool('ffmpeg')))
            for msg in self.outbox:
                self.ui_core.PostWebMessageAsJson(msg)
            self.outbox.clear()
            if self.core:
                self.send('browser', url=self.core.Source)
        elif cmd == 'findUrl':
            self.find_url(m.get('url'))
        elif cmd == 'openInBrowser':
            self.open_in_browser(m.get('url'))
        elif cmd == 'findPage':
            self.find_page()
        elif cmd == 'navigate':
            self.navigate(m.get('url'))
        elif cmd in ('back', 'forward', 'reload') and self.core:
            {'back': self.core.GoBack, 'forward': self.core.GoForward, 'reload': self.core.Reload}[cmd]()
        elif cmd == 'slot':
            self.place_browser(m)
        elif cmd == 'enqueue':
            if m.get('from') == 'captured':
                self.enqueue_captured(m.get('ids') or [])
            else:
                self.add_jobs([self.cands[int(i)] for i in m.get('ids') or [] if int(i) < len(self.cands)])
        elif cmd == 'cancel':
            self.cancel_jobs(m.get('ids') or [])
        elif cmd == 'retry':
            self.retry_jobs(m.get('ids') or [])
        elif cmd == 'clear':
            self.clear_jobs()
        elif cmd == 'openFolder':
            self.open_folder(m.get('id'))
        elif cmd == 'browse':
            self.browse()
        elif cmd == 'set':
            self.set_option(m.get('key'), m.get('value'))

    def place_browser(self, m):
        """Lay the browser over the page's 「ブラウザ」 area (device pixels, from the page)."""
        if m.get('visible') and m.get('w', 0) > 0 and m.get('h', 0) > 0:
            self.web.SetBounds(int(m['x']), int(m['y']), int(m['w']), int(m['h']))
            self.web.Visible = True
            self.web.BringToFront()
        else:
            self.web.Visible = False

    def log(self, msg):
        self.send('log', text=msg)

    def status(self, text):
        self.send('status', text=text)

    # ---- browser ----------------------------------------------------------------------------------
    def on_navigation(self, sender, e):
        if not e.IsRedirected:  # a new page: what the previous one loaded no longer counts
            self.capture.reset()
            self.send('capturedReset')

    def on_response(self, sender, e):
        """Every response the browser receives (all frames). Cheap unless it is a video."""
        uri = e.Request.Uri
        mime = e.Response.Headers.GetHeader('Content-Type') if e.Response.Headers.Contains('Content-Type') else ''
        if not is_media(uri, mime):
            return
        m = self.capture.add(uri, mime, {kv.Key: kv.Value for kv in e.Request.Headers})
        if m:
            kind = kind_of(m.url)
            self.send('captured', item={'id': m.url, 'url': m.url, 'mime': m.mime or '—',
                                        'kind': 'mp4' if kind == '動画' else kind,
                                        'time': time.strftime('%H:%M:%S', time.localtime(m.at))})

    def on_new_window(self, sender, e):
        """Links that open a new tab stay in this browser (so their videos are seen). Sized pop-ups
        (sign-in windows) open as pop-ups."""
        if not e.WindowFeatures.HasSize:
            e.Handled = True
            self.core.Navigate(e.Uri)

    def navigate(self, url):
        url = normalized(url)
        if not url or not self.core:
            return
        try:
            self.core.Navigate(url)
        except Exception as e:  # noqa: BLE001 - bad URL
            self.log(f'開けませんでした: {url} ({e})')

    def open_in_browser(self, url):
        url = normalized(url)
        if url:
            self.navigate(url)
            self.send('tab', name='browser')

    def clipboard_url(self):
        try:
            text = (Clipboard.GetText() or '').strip()
        except Exception:  # noqa: BLE001
            return None
        return text if text.startswith(('http://', 'https://')) and '\n' not in text else None

    def when_done(self, tasks, then):
        """Run then() on the UI thread once every .NET Task has finished (without blocking the UI)."""
        timer = Timer()
        timer.Interval = 50
        self.timers.add(timer)

        def tick(s, e):
            if all(t.IsCompleted for t in tasks):
                timer.Stop()
                self.timers.discard(timer)
                then()

        timer.Tick += tick
        timer.Start()

    def cookies(self, urls, then):
        """then({url: Cookie header}) with the browser's cookies for each URL."""
        tasks = {u: self.core.CookieManager.GetCookiesAsync(u) for u in urls}

        def ready():
            then({u: '; '.join(f'{c.Name}={c.Value}' for c in t.Result)
                  for u, t in tasks.items() if not t.IsFaulted and t.Result is not None})

        self.when_done(list(tasks.values()), ready)

    # ---- search -----------------------------------------------------------------------------------
    def find_url(self, url):
        """yt-dlp and the page's HTML, fetched by us (no browser)."""
        url = normalized(url)
        if self.searching or not url:
            return
        self.send('url', url=url)
        self.set_searching(True)
        self.log(f'--- {url}')
        log = lambda m: self.q.put(('log', m))  # noqa: E731

        def work():
            try:
                self.q.put(('found', find_videos(url, log)))
            except Blocked:
                self.q.put(('blocked', url))
            except Exception as e:  # noqa: BLE001 - shown to the user
                self.q.put(('log', f'エラー: {e}'))
                self.q.put(('found', []))

        threading.Thread(target=work, daemon=True).start()

    def find_page(self):
        """The page open in the browser: what it loaded, its DOM now, and yt-dlp with its cookies."""
        if self.searching:
            return
        core = self.core
        page_url = core.Source if core else ''
        if not page_url.startswith('http'):
            self.status('「ブラウザ」タブでページを開いてから押してください')
            self.send('tab', name='browser')
            return
        title, ua = core.DocumentTitle, core.Settings.UserAgent
        self.set_searching(True)
        self.log(f'--- {page_url}（ブラウザ）')
        html_task = core.ExecuteScriptAsync('document.documentElement.outerHTML')  # the page as it is now

        def with_cookies(cookies):
            html = ''
            if not html_task.IsFaulted:
                try:
                    html = json.loads(html_task.Result) or ''
                except (ValueError, TypeError):
                    pass
            captured = self.capture.candidates(page_url, title, ua, cookies.get)
            headers = {'User-Agent': ua} | ({'Cookie': cookies[page_url]} if cookies.get(page_url) else {})
            log = lambda m: self.q.put(('log', m))  # noqa: E731

            def work():
                try:
                    self.q.put(('found', find_in_page(page_url, title, html, captured, headers, log)))
                except Exception as e:  # noqa: BLE001 - shown to the user
                    self.q.put(('log', f'エラー: {e}'))
                    self.q.put(('found', []))

            threading.Thread(target=work, daemon=True).start()

        self.when_done([html_task], lambda: self.cookies([page_url, *list(self.capture.media)[:40]], with_cookies))

    def set_searching(self, on):
        self.searching = on
        self.send('busy', on=on, text='探しています…' if on else None)

    def show(self, cands):
        self.set_searching(False)
        self.cands = cands
        self.send('found', items=[{'id': str(i), 'title': c.title, 'url': c.url, 'kind': c.kind,
                                   'resolution': c.resolution or '—', 'duration': fmt_duration(c.duration),
                                   'size': fmt_size(c.size, c.size_approx), 'source': c.source}
                                  for i, c in enumerate(cands)])
        self.send('tab', name='found')
        self.status(f'{len(cands)} 件見つかりました。選んで「ダウンロードに追加」を押してください' if cands else
                    '動画が見つかりませんでした。「ブラウザで開く」で動画を再生してから、「ブラウザのページから探す」を試してください')

    def poll(self):
        try:
            while True:
                kind, v = self.q.get_nowait()
                if kind == 'log':
                    self.log(v)
                elif kind == 'found':
                    self.show(v)
                elif kind == 'blocked':
                    self.set_searching(False)
                    self.status('Cloudflare の確認画面（ボット対策）で止められました')
                    self.send('blocked', url=v)
                elif kind == 'job':
                    job_id, frac, text = v
                    job = self.jobs.get(job_id)
                    if job:
                        job.progress, job.text = frac, text
                        self.send_job(job)
                elif kind == 'job-end':
                    job_id, state, text = v
                    job = self.jobs.get(job_id)
                    if job:
                        self.set_job_state(job, state, text, 1.0 if state == DONE else job.progress)
        except queue.Empty:
            pass
        self.start_jobs()

    # ---- download queue -----------------------------------------------------------------------------
    def send_job(self, job):
        c = job.cand
        self.send('job', item={'id': job.id, 'title': c.title, 'quality': job.quality, 'state': job.state,
                               'size': fmt_size(c.size, c.size_approx), 'text': job.text or job.state,
                               'progress': job.progress})

    def set_job_state(self, job, state, text=None, progress=None):
        job.state, job.text, job.progress = state, text or state, progress
        self.send_job(job)
        self.send_stats()

    def send_stats(self):
        states = [j.state for j in self.jobs.values()]
        self.send('queueStats', parts=[[s, states.count(s)] for s in (RUNNING, WAITING, DONE, FAILED, CANCELLED)
                                       if states.count(s)])

    def start_jobs(self):
        """Start waiting jobs, oldest first, up to the chosen number at once."""
        running = sum(j.state == RUNNING for j in self.jobs.values())
        for job in self.jobs.values():
            if running >= self.parallel:
                break
            if job.state == WAITING:
                self.set_job_state(job, RUNNING, '開始しています…')
                threading.Thread(target=self.run_job, args=(job,), daemon=True).start()
                running += 1

    def run_job(self, job):
        title = job.cand.title
        log = lambda m: self.q.put(('log', f'[{title[:30]}] {m}'))  # noqa: E731

        def progress(frac, text):
            self.q.put(('job', (job.id, frac, (f'{frac * 100:.0f}%  ' if frac is not None else '') + text)))

        try:
            job.files = download(job.cand, job.outdir, job.quality, log, progress, job.stop)
        except Cancelled:
            self.q.put(('job-end', (job.id, CANCELLED, None)))
            return
        except Exception as e:  # noqa: BLE001 - shown in the list and the log
            msg = str(e).replace('ERROR: ', '')
            self.q.put(('log', f'失敗: {title}: {msg}'))
            self.q.put(('job-end', (job.id, FAILED, f'{FAILED}: {msg[:120]}')))
            return
        for p in job.files:
            self.q.put(('log', f'保存しました: {p}'))
        self.q.put(('job-end', (job.id, DONE, None)))

    def enqueue_captured(self, urls):
        """As the browser loaded them (no check), with the cookies it sent or holds."""
        if not urls or not self.core:
            return
        core = self.core
        page_url, title, ua = core.Source, core.DocumentTitle, core.Settings.UserAgent

        def ready(cookies):
            cands = self.capture.candidates(page_url, title, ua, cookies.get, set(urls))
            for c in cands:
                c.source = 'ブラウザ（未確認）'
            self.add_jobs(cands)

        self.cookies(urls, ready)

    def add_jobs(self, sel):
        active = {(j.cand.url, j.quality) for j in self.jobs.values() if j.state in (WAITING, RUNNING)}
        added = 0
        for c in sel:
            if (c.url, self.quality) in active:
                self.log(f'追加済みです: {c.title}')
                continue
            job = Job(next(self.job_ids), c, self.outdir, self.quality)
            self.jobs[job.id] = job
            self.send_job(job)
            added += 1
        if added:
            self.send('toast', text=f'{added} 件をダウンロード一覧に追加しました')
        self.send_stats()
        self.save()
        self.start_jobs()

    def cancel_jobs(self, ids):
        ids = [i for i in ids if i in self.jobs] or [j.id for j in self.jobs.values() if j.state in (WAITING, RUNNING)]
        for job in (self.jobs[i] for i in ids):
            if job.state == WAITING:
                self.set_job_state(job, CANCELLED)
            elif job.state == RUNNING:
                job.stop.set()
                job.text = '中止しています…'
                self.send_job(job)

    def retry_jobs(self, ids):
        for job in (self.jobs[i] for i in ids if i in self.jobs):
            if job.state in (FAILED, CANCELLED):
                job.stop = threading.Event()
                self.set_job_state(job, WAITING)
        self.start_jobs()

    def clear_jobs(self):
        gone = [j.id for j in self.jobs.values() if j.state in (DONE, FAILED, CANCELLED)]
        for i in gone:
            del self.jobs[i]
        self.send('jobsRemoved', ids=gone)
        self.send_stats()

    def browse(self):
        dlg = FolderBrowserDialog()
        dlg.SelectedPath = self.outdir
        if dlg.ShowDialog(self.form) == DialogResult.OK:
            self.set_option('outdir', dlg.SelectedPath)
            self.send('outdir', value=self.outdir)

    def open_folder(self, job_id=None):
        d = self.jobs[job_id].outdir if job_id in self.jobs else self.outdir
        os.makedirs(d, exist_ok=True)
        os.startfile(d)  # noqa: S606

    def set_option(self, key, value):
        if key == 'outdir':
            self.outdir = (value or '').strip() or default_outdir()
        elif key == 'quality' and value in QUALITIES:
            self.quality = value
        elif key == 'parallel' and str(value) in PARALLEL:
            self.parallel = int(value)
            self.start_jobs()
        self.save()

    def save(self):
        save_settings({'outdir': self.outdir, 'quality': self.quality, 'parallel': self.parallel,
                       'last_url': self.core.Source if self.core else self.settings.get('last_url', '')})

    def on_closing(self, sender, e):
        left = sum(j.state in (WAITING, RUNNING) for j in self.jobs.values())
        if left and MessageBox.Show(f'ダウンロードが {left} 件残っています。中止して終了しますか？', APP,
                                    MessageBoxButtons.YesNo) != DialogResult.Yes:
            e.Cancel = True
            return
        for job in self.jobs.values():
            job.stop.set()
        self.save()


def run(setup=None, start_url=None):
    """Show the window on an STA thread (WinForms / WebView2 need one). setup(app) runs before it shows."""
    result = {}

    def ui():
        try:
            Application.EnableVisualStyles()
            app = App(start_url)
            if setup:
                setup(app)
            Application.Run(app.form)
        except BaseException as e:  # noqa: BLE001 - reported by the caller
            result['error'] = e

    if os.name == 'nt':
        try:  # sharp text on high-DPI screens
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
    t = Thread(ThreadStart(ui))
    t.SetApartmentState(ApartmentState.STA)
    t.Start()
    t.Join()
    if 'error' in result:
        raise result['error']


def main():
    run()
