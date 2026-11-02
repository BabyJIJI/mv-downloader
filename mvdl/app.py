"""The window. The interface is a web page (ui/index.html) in one WebView2; the built-in browser is a second
WebView2 laid over the page's 「ブラウザ」 tab. They talk over postMessage: the page sends commands
({cmd: ...}), this side sends updates ({type: ...}).

「URL から探す」 looks at a URL with yt-dlp and the page's HTML. The built-in browser is for pages that need
a person: pass Cloudflare, log in, play the video, then 「ブラウザのページから探す」. While browsing, the
browser only reports what it loaded (capture.py); the page is not touched until the button is pressed
(then the page's server buttons are pressed in turn when it has them: servers.py).
WinForms + WebView2 through pythonnet; Windows only.
"""
import itertools
import json
import os
import queue
import sys
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import clr  # pythonnet

from . import __version__
from . import probe, servers
from .capture import Capture, is_media
from .downloader import QUALITIES, Cancelled, download
from .finder import Blocked, NeedsBrowser, find_in_page, find_videos, fmt_duration, fmt_size, kind_of
from .jobs import (CANCELLED, DONE, FAILED, PAUSED, RESTORED, RUNNING, UNFINISHED, WAITING, Job, load_queue,
                   remove_partials, save_queue)
from .tools import find_tool

clr.AddReference('System.Windows.Forms')
clr.AddReference('System.Drawing')
HERE = os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'mvdl')
LIB, UI = os.path.join(HERE, 'lib'), os.path.join(HERE, 'ui')
clr.AddReference(os.path.join(LIB, 'Microsoft.Web.WebView2.Core.dll'))
clr.AddReference(os.path.join(LIB, 'Microsoft.Web.WebView2.WinForms.dll'))

from Microsoft.Web.WebView2.Core import CoreWebView2Environment, CoreWebView2HostResourceAccessKind  # noqa: E402
from Microsoft.Web.WebView2.WinForms import WebView2  # noqa: E402
from System.Drawing import Color, Size  # noqa: E402
from System.Threading import ApartmentState, Thread, ThreadStart  # noqa: E402
from System.Windows.Forms import (  # noqa: E402
    Application, Clipboard, DialogResult, DockStyle, FolderBrowserDialog, Form, MessageBox, MessageBoxButtons,
    MessageBoxIcon, Timer)

APP = '動画ダウンローダー'
DATA = os.path.join(os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'mvdl')
SETTINGS = os.path.join(os.environ.get('APPDATA') or DATA, 'mvdl', 'settings.json')
WEBVIEW2_RUNTIME = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
PARALLEL = tuple(str(n) for n in range(1, 11))
QUEUE = os.path.join(os.path.dirname(SETTINGS), 'queue.json')  # the download queue, kept between runs


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


class BrowserTab:
    """One tab of the built-in browser: its own WebView2 (sharing the profile) and what it loaded."""

    def __init__(self, app, tid, url=None, new_window=None):
        self.app, self.id = app, tid
        self.capture = Capture()
        self.core = None
        self.probing = False  # pressing the page's server buttons: no pop-ups, no leaving the page
        self.url, self.new_window = url, new_window  # what to show once ready
        self.web = WebView2()
        self.web.Visible = False
        self.web.CoreWebView2InitializationCompleted += self.on_ready
        app.form.Controls.Add(self.web)
        self.web.BringToFront()
        self.web.EnsureCoreWebView2Async(app.env)

    def on_ready(self, sender, e):
        app = self.app
        if not e.IsSuccess:
            app.send('browserFailed', text=f'ブラウザを使えません: {e.InitializationException}')
            app.log(f'ブラウザのタブを開けませんでした: {e.InitializationException}')
            app.status(f'ブラウザのタブを開けませんでした: {e.InitializationException}')
            return
        core = self.core = self.web.CoreWebView2
        core.WebResourceResponseReceived += lambda s, a: app.on_response(self, a)
        core.NavigationStarting += lambda s, a: app.on_navigation(self, a)
        core.SourceChanged += lambda s, a: app.tab_changed(self)
        core.DocumentTitleChanged += lambda s, a: app.tab_changed(self)
        core.NewWindowRequested += lambda s, a: app.on_new_window(self, a)
        core.WindowCloseRequested += lambda s, a: app.close_tab(self.id)  # window.close()
        if self.new_window:  # a link / window.open of another tab: this tab is that new window
            args, deferral = self.new_window
            args.NewWindow = core
            args.Handled = True
            deferral.Complete()
        elif self.url:
            app.navigate(self.url, self)
        app.tab_ready(self)

    @property
    def title(self):
        if not self.core:
            return '読み込み中…'
        return self.core.DocumentTitle or self.core.Source or '新しいタブ'

    @property
    def source(self):
        return self.core.Source if self.core else (self.url or '')

    def close(self):
        self.web.Visible = False
        self.app.form.Controls.Remove(self.web)
        self.web.Dispose()


class App:
    def __init__(self, start_url=None):
        self.q = queue.Queue()        # (kind, payload) from worker threads, drained by a UI timer
        self.checked = {}             # (tab id, captured url) -> Candidate after the check (resolution / size ...)
        self.checker = ThreadPoolExecutor(2)  # checks of what the browser loaded: two at a time
        self.searching = False
        self.cands = []
        restored = load_queue(QUEUE)  # the queue from last time; unfinished ones come back paused
        self.jobs = {j.id: j for j in restored}  # id -> Job, in the order they were added
        first = max((int(j.id[1:]) for j in restored if j.id[1:].isdigit()), default=-1) + 1
        self.job_ids = (f'j{n}' for n in itertools.count(first))
        self.browsers = {}            # tab id -> BrowserTab, in the order they were opened
        self.active = None            # id of the tab shown
        self.tab_ids = (f'b{n}' for n in itertools.count())
        self.slot = {}                # where the page wants the browser (device pixels)
        self.env = None               # one WebView2 environment for the interface and every tab
        self.env_error = None         # why the browser could not start
        self.pending_tabs = []        # tabs asked for before the browser was ready: (url, activate)
        self.timers = set()           # keep task-wait timers alive
        self.ui_core = None           # CoreWebView2 of the interface page
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
        self.ui = WebView2()
        self.ui.Dock = DockStyle.Fill
        self.ui.DefaultBackgroundColor = f.BackColor
        self.ui.CoreWebView2InitializationCompleted += self.on_ui_ready
        f.Controls.Add(self.ui)

        f.Load += lambda s, e: self.on_load()
        f.HandleCreated += lambda s, e: self.dark_title_bar()
        f.FormClosing += self.on_closing
        self.ticker = Timer()
        self.ticker.Interval = 100
        self.ticker.Tick += lambda s, e: self.poll()
        self.ticker.Start()

    # the tab shown (the search, the address bar and the tests work on it)
    @property
    def tab(self):
        return self.browsers.get(self.active)

    @property
    def core(self):
        return self.tab.core if self.tab else None

    @property
    def web(self):
        return self.tab.web if self.tab else None

    @property
    def capture(self):
        return self.tab.capture if self.tab else Capture()

    # ---- start ------------------------------------------------------------------------------------
    def on_load(self):
        try:
            CoreWebView2Environment.SetLoaderDllFolderPath(LIB)
            # cookies / logins / passed checks persist here, shared by every tab
            task = CoreWebView2Environment.CreateAsync(None, os.path.join(DATA, 'webview2'), None)
        except Exception as e:  # noqa: BLE001
            self.browser_missing(e)
            return

        def ready():
            if task.IsFaulted:
                self.env_error = str(task.Exception)
                self.browser_missing(task.Exception)
                return
            self.env = task.Result
            self.ui.EnsureCoreWebView2Async(self.env)
            asked, self.pending_tabs = self.pending_tabs, []
            # the tabs from last time, then what was asked for while starting (that one is shown)
            urls = [u for u in self.settings.get('tabs') or [self.settings.get('last_url')] if u]
            for u in urls or ([] if asked else [None]):
                self.new_tab(u, activate=False)
            for u, activate in asked:
                self.new_tab(u, activate=activate)
            if self.active is None and self.browsers:
                self.activate(next(iter(self.browsers)))

        self.when_done([task], ready)

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

    def tab_ready(self, tab):
        if len(self.browsers) == 1 and tab.id == self.active and tab.core:
            self.log(f'ブラウザ: WebView2 {tab.core.Environment.BrowserVersionString}')
            if os.environ.get('MVDL_STARTUP_CHECK'):  # build check: the exe starts and its browser comes up
                with open(os.environ['MVDL_STARTUP_CHECK'], 'w', encoding='utf-8') as fh:
                    fh.write(tab.core.Environment.BrowserVersionString)
                self.form.Close()
                return
        self.tab_changed(tab)

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
            self.send_tabs()
            for job in self.jobs.values():
                self.send_job(job)
            self.send_stats()
            resumable = sum(j.text == RESTORED for j in self.jobs.values())
            if resumable:
                self.send('toast', text=f'前回の続きが {resumable} 件あります（ダウンロード一覧で「再開」）')
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
        elif cmd == 'newTab':
            self.new_tab(m.get('url'))
        elif cmd == 'closeTab':
            self.close_tab(m.get('id'))
        elif cmd == 'switchTab':
            self.activate(m.get('id'))
        elif cmd == 'slot':
            self.slot = m
            self.place_browser()
        elif cmd == 'enqueue':
            if m.get('from') == 'captured':
                self.enqueue_captured(m.get('ids') or [])
            else:
                self.add_jobs([self.cands[int(i)] for i in m.get('ids') or [] if int(i) < len(self.cands)])
        elif cmd == 'cancel':
            self.cancel_jobs(m.get('ids') or [])
        elif cmd == 'retry':
            self.retry_jobs(m.get('ids') or [])
        elif cmd == 'pause':
            self.pause_jobs(m.get('ids') or [])
        elif cmd == 'resume':
            self.resume_jobs(m.get('ids') or [])
        elif cmd == 'clear':
            self.clear_jobs()
        elif cmd == 'openFolder':
            self.open_folder(m.get('id'))
        elif cmd == 'browse':
            self.browse()
        elif cmd == 'set':
            self.set_option(m.get('key'), m.get('value'))

    def place_browser(self):
        """Lay the shown tab over the page's 「ブラウザ」 area (device pixels, from the page); hide the rest."""
        m = self.slot
        show = m.get('visible') and m.get('w', 0) > 0 and m.get('h', 0) > 0
        for tab in self.browsers.values():
            if show and tab.id == self.active:
                tab.web.SetBounds(int(m['x']), int(m['y']), int(m['w']), int(m['h']))
                tab.web.Visible = True
                tab.web.BringToFront()
            else:
                tab.web.Visible = False

    def log(self, msg):
        self.send('log', text=msg)

    def status(self, text):
        self.send('status', text=text)

    # ---- browser tabs -------------------------------------------------------------------------------
    def new_tab(self, url=None, new_window=None, activate=True):
        if self.env is None:
            if self.env_error:
                self.status(f'ブラウザを使えません: {self.env_error}')
            elif new_window is None:  # opened as soon as the browser is ready
                self.pending_tabs.append((url, activate))
                self.status('ブラウザを準備しています…（準備ができたら開きます）')
            return None
        tab = BrowserTab(self, next(self.tab_ids), normalized(url) if url else None, new_window)
        self.browsers[tab.id] = tab
        if activate:
            self.activate(tab.id)
        else:
            self.send_tabs()
        return tab

    def close_tab(self, tid):
        tab = self.browsers.pop(tid, None)
        if not tab:
            return
        for key in [k for k in self.checked if k[0] == tid]:
            del self.checked[key]
        self.send('capturedRemove', tab=tid)
        tab.close()
        if self.active == tid:
            ids = list(self.browsers)
            self.active = None
            if ids:
                self.activate(ids[-1])
        self.send_tabs()
        self.place_browser()

    def activate(self, tid):
        if tid not in self.browsers:
            return
        self.active = tid
        self.place_browser()
        self.send_tabs()
        if self.tab.source.startswith('http'):
            self.send('pageUrl', url=self.tab.source)

    def send_tabs(self):
        self.send('browserTabs', active=self.active,
                  tabs=[{'id': t.id, 'title': t.title, 'url': t.source} for t in self.browsers.values()])
        self.send('browser', url=self.tab.source if self.tab else '')

    def tab_changed(self, tab):
        self.send_tabs()
        if tab.id == self.active and tab.source.startswith('http'):
            self.send('pageUrl', url=tab.source)  # the console's URL follows the page shown

    def on_navigation(self, tab, e):
        if tab.probing:  # a server button tried to leave the page
            e.Cancel = True
            return
        if not e.IsRedirected:  # a new page: what the previous one loaded no longer counts
            tab.capture.reset()
            for key in [k for k in self.checked if k[0] == tab.id]:
                del self.checked[key]
            self.send('capturedRemove', tab=tab.id)

    def on_response(self, tab, e):
        """Every response a tab receives (all frames). Cheap unless it is a video."""
        uri = e.Request.Uri
        mime = e.Response.Headers.GetHeader('Content-Type') if e.Response.Headers.Contains('Content-Type') else ''
        if not is_media(uri, mime):
            return
        m = tab.capture.add(uri, mime, {kv.Key: kv.Value for kv in e.Request.Headers})
        if m:
            self.send_captured(tab, m, {'check': '確認中…'})
            [cand] = tab.capture.candidates(tab.core.Source, tab.core.DocumentTitle, tab.core.Settings.UserAgent,
                                            urls={m.url})
            self.checker.submit(self.check_captured, tab, m, cand)

    def send_captured(self, tab, m, info):
        kind = kind_of(m.url)
        self.send('captured', item={'id': f'{tab.id}|{m.url}', 'tab': tab.title, 'url': m.url, 'mime': m.mime or '—',
                                    'kind': 'mp4' if kind == '動画' else kind,
                                    'time': time.strftime('%H:%M:%S', time.localtime(m.at)),
                                    'resolution': '—', 'duration': '—', 'size': '—', 'note': ''} | info)

    def check_captured(self, tab, m, cand):
        """Worker: resolution / duration / size of one captured video (same check as the search)."""
        notes = []
        try:
            c, _ = probe.check(cand, notes.append)
        except Exception as e:  # noqa: BLE001
            c, notes = None, [str(e)]
        self.q.put(('checked', (tab, m, c, notes[-1] if notes else '')))

    def on_new_window(self, tab, e):
        """A link to a new tab / window.open: open it as another tab, as that very window (window.opener
        and sign-in pop-ups keep working), so its videos are seen too. It opens behind the tab shown:
        video sites open ad pop-ups on the first click on the player, and the search must stay on the
        video's page."""
        if tab.probing:  # an ad pop-up of a server button
            e.Handled = True
            return
        deferral = e.GetDeferral()
        if not self.new_tab(new_window=(e, deferral), activate=False):
            deferral.Complete()
            return
        self.log(f'新しいタブを裏で開きました（広告かもしれません）: {e.Uri}')
        self.send('toast', text='新しいタブを裏で開きました（上のタブから切り替えられます）')

    def navigate(self, url, tab=None):
        url = normalized(url)
        if not url:
            return
        tab = tab or self.tab
        if not tab:
            self.new_tab(url)
            return
        if not tab.core:
            tab.url = url
            return
        try:
            tab.core.Navigate(url)
        except Exception as e:  # noqa: BLE001 - bad URL
            self.log(f'開けませんでした: {url} ({e})')

    def open_in_browser(self, url):
        """In a new tab (the open ones stay as they are). Without a URL: a blank tab to type into."""
        url = normalized(url)
        self.log(f'ブラウザで開きます: {url}' if url else 'ブラウザで新しいタブを開きます')
        self.new_tab(url or None)
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

    def cookies(self, core, urls, then):
        """then({url: Cookie header}) with the browser's cookies for each URL."""
        tasks = {u: core.CookieManager.GetCookiesAsync(u) for u in urls}

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
            except NeedsBrowser as nb:
                self.q.put(('needsBrowser', (nb.url, nb.servers)))
            except Exception as e:  # noqa: BLE001 - shown to the user
                self.q.put(('log', f'エラー: {e}'))
                self.q.put(('found', []))

        threading.Thread(target=work, daemon=True).start()

    def find_page(self):
        """The page open in the browser: its server buttons pressed in turn (when it has them), what it
        loaded, its DOM then, and yt-dlp with its cookies."""
        if self.searching:
            return
        tab = self.tab
        core = tab.core if tab else None
        page_url = core.Source if core else ''
        if not page_url.startswith('http'):
            self.status('「ブラウザ」タブでページを開いてから押してください')
            self.send('tab', name='browser')
            return
        self.set_searching(True)
        self.log(f'--- {page_url}（ブラウザ）')
        tab.probing = True
        start = core.ExecuteScriptAsync(servers.SCRIPT)

        def started():
            n = 0
            if not start.IsFaulted:
                try:
                    n = int(json.loads(start.Result) or 0)
                except (ValueError, TypeError):
                    pass
            if not n:
                tab.probing = False
                self.find_page_now(tab, page_url, [])
                return
            self.log(f'サーバーの切り替えボタンが {n} 個あります。順に押してプレーヤーを開きます…')
            self.send('busy', on=True, text=f'サーバーのボタンを順に押しています（{n} 個）…')
            self.poll_servers(tab, page_url, time.time() + servers.TIMEOUT_S)

        self.when_done([start], started)

    def poll_servers(self, tab, page_url, deadline):
        """Wait (without blocking the UI) for servers.SCRIPT to finish on the page."""
        if self.browsers.get(tab.id) is not tab or not tab.core:
            tab.probing = False
            self.set_searching(False)
            return
        task = tab.core.ExecuteScriptAsync(servers.RESULT)

        def ready():
            result = None
            if not task.IsFaulted:
                try:
                    result = json.loads(json.loads(task.Result) or 'null')
                except (ValueError, TypeError):
                    pass
            if result is not None and not result.get('done') and time.time() < deadline:
                self.later(400, lambda: self.poll_servers(tab, page_url, deadline))
                return
            tab.probing = False
            if result is None or not result.get('done'):
                self.log('サーバーのボタンを押し終わりませんでした（ここまでの分で探します）')
            if result and result.get('error'):
                self.log(f'サーバーのボタンを押せませんでした: {result["error"]}')
            for item in (result or {}).get('items') or []:
                if not item.get('urls'):
                    self.log(f'サーバー「{item.get("label") or "?"}」: 押してもプレーヤーは出ませんでした')
            self.find_page_now(tab, page_url, servers.players(result))

        self.when_done([task], ready)

    def later(self, ms, fn):
        timer = Timer()
        timer.Interval = ms
        self.timers.add(timer)

        def tick(s, e):
            timer.Stop()
            self.timers.discard(timer)
            fn()

        timer.Tick += tick
        timer.Start()

    def find_page_now(self, tab, page_url, players):
        core = tab.core
        if self.browsers.get(tab.id) is not tab or not core:
            self.set_searching(False)
            return
        self.send('busy', on=True, text='探しています…')
        title, ua = core.DocumentTitle, core.Settings.UserAgent
        html_task = core.ExecuteScriptAsync('document.documentElement.outerHTML')  # the page as it is now

        def with_cookies(cookies):
            html = ''
            if not html_task.IsFaulted:
                try:
                    html = json.loads(html_task.Result) or ''
                except (ValueError, TypeError):
                    pass
            captured = tab.capture.candidates(page_url, title, ua, cookies.get)
            headers = {'User-Agent': ua} | ({'Cookie': cookies[page_url]} if cookies.get(page_url) else {})
            log = lambda m: self.q.put(('log', m))  # noqa: E731

            def work():
                try:
                    self.q.put(('found', find_in_page(page_url, title, html, captured, headers, log, players)))
                except Exception as e:  # noqa: BLE001 - shown to the user
                    self.q.put(('log', f'エラー: {e}'))
                    self.q.put(('found', []))

            threading.Thread(target=work, daemon=True).start()

        self.when_done([html_task], lambda: self.cookies(core, [page_url, *list(tab.capture.media)[:40]], with_cookies))

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
                elif kind == 'needsBrowser':
                    self.set_searching(False)
                    url, names = v
                    self.status('動画の URL は、サーバーのボタンを押したときにサイトのスクリプトが作ります')
                    self.send('needsBrowser', url=url, servers=names)
                elif kind == 'checked':
                    tab, m, c, note = v
                    if self.browsers.get(tab.id) is tab and tab.capture.media.get(m.url) is m:  # still that page
                        if c is not None:
                            self.checked[(tab.id, m.url)] = c
                        self.send_captured(tab, m, {
                            'check': '動画ではない' if c is None else '未確認' if '未確認' in c.source else '確認済み',
                            'note': note,
                            **({} if c is None else {'resolution': c.resolution or '—',
                                                     'duration': fmt_duration(c.duration),
                                                     'size': fmt_size(c.size, c.size_approx)})})
                elif kind == 'job':
                    job_id, frac, text = v
                    job = self.jobs.get(job_id)
                    if job:
                        job.progress, job.text = frac, text
                        self.send_job(job)
                        self.update_title()
                elif kind == 'job-end':
                    job_id, state, text = v
                    job = self.jobs.get(job_id)
                    if job:
                        self.set_job_state(job, state, text, 1.0 if state == DONE else job.progress)
                        job.pausing = False
        except queue.Empty:
            pass
        self.start_jobs()

    # ---- download queue -----------------------------------------------------------------------------
    def send_job(self, job):
        c = job.cand
        host = urllib.parse.urlparse(c.url).hostname or ''
        page = urllib.parse.urlparse(c.headers.get('Referer') or '').hostname or ''
        self.send('job', item={'id': job.id, 'title': c.title, 'quality': job.quality, 'state': job.state,
                               'size': fmt_size(c.size, c.size_approx), 'text': job.text or job.state,
                               'progress': job.progress, 'host': host, 'url': c.url,
                               'page': page if page and page != host else ''})

    def set_job_state(self, job, state, text=None, progress=None):
        job.state, job.text, job.progress = state, text or state, progress
        self.send_job(job)
        self.send_stats()
        self.save_jobs()

    def send_stats(self):
        states = [j.state for j in self.jobs.values()]
        self.send('queueStats', parts=[[s, states.count(s)] for s in (RUNNING, WAITING, PAUSED, DONE, FAILED, CANCELLED)
                                       if states.count(s)])
        self.update_title()

    def update_title(self):
        """The window title shows the downloads, not the page in the browser:
        「動画ダウンローダー — ダウンロード中 2・待機 3（45%）」, just the name when nothing is going on."""
        states = [j.state for j in self.jobs.values()]
        if not any(s in (RUNNING, WAITING, PAUSED) for s in states):
            text = APP + (f' — 完了 {states.count(DONE)}' if DONE in states else '')
        else:
            parts = [f'{s} {states.count(s)}' for s in (RUNNING, WAITING, PAUSED, FAILED) if s in states]
            known = [j.progress for j in self.jobs.values() if j.state == RUNNING and j.progress is not None]
            pct = f'（{sum(known) / len(known) * 100:.0f}%）' if known else ''
            text = f'{APP} — {"・".join(parts)}{pct}'
        if self.form.Text != text:
            self.form.Text = text

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
            job.files = download(job.cand, job.outdir, job.quality, log, progress, job.stop,
                                 lambda name: name in job.partials or job.partials.append(name))
        except Cancelled:
            if job.pausing:  # keep the progress: yt-dlp continues the .part file on resume
                self.q.put(('job-end', (job.id, PAUSED, f'{PAUSED}（{job.text}）' if job.text else PAUSED)))
            else:
                self.drop_partials(job, log)
                self.q.put(('job-end', (job.id, CANCELLED, None)))
            return
        except Exception as e:  # noqa: BLE001 - shown in the list and the log
            msg = str(e).replace('ERROR: ', '')
            self.q.put(('log', f'失敗: {title}: {msg}'))
            self.q.put(('job-end', (job.id, FAILED, f'{FAILED}: {msg[:120]}')))
            return
        for p in job.files:
            self.q.put(('log', f'保存しました: {p}'))
        job.partials = []
        self.q.put(('job-end', (job.id, DONE, None)))

    def drop_partials(self, job, log=None):
        removed = remove_partials(job.partials)
        job.partials = []
        if removed:
            (log or self.log)(f'途中のファイルを削除しました: {", ".join(os.path.basename(p) for p in removed)}')

    def enqueue_captured(self, ids):
        """As the browser loaded them, with the cookies it sent or holds. ids: "<tab id>|<url>"."""
        by_tab = {}
        for i in ids:
            tid, _, url = i.partition('|')
            if tid in self.browsers and self.browsers[tid].core:
                by_tab.setdefault(tid, []).append(url)
        for tid, urls in by_tab.items():
            self.enqueue_from_tab(self.browsers[tid], urls)

    def enqueue_from_tab(self, tab, urls):
        core = tab.core
        page_url, title, ua = core.Source, core.DocumentTitle, core.Settings.UserAgent

        def ready(cookies):
            cands = tab.capture.candidates(page_url, title, ua, cookies.get, set(urls))
            for c in cands:
                done = self.checked.get((tab.id, c.url))
                if done:  # what the check found (size for the list)
                    c.resolution, c.duration, c.size, c.size_approx = (done.resolution, done.duration, done.size,
                                                                       done.size_approx)
                    c.source = done.source
                else:
                    c.source = 'ブラウザ（未確認）'
            self.add_jobs(cands)

        self.cookies(core, urls, ready)

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
            self.save_jobs()
        self.send_stats()
        self.save()
        self.start_jobs()

    def cancel_jobs(self, ids):
        """Stop and delete the partial files (the page asked for confirmation first)."""
        ids = [i for i in ids if i in self.jobs] or [j.id for j in self.jobs.values() if j.state in UNFINISHED]
        for job in (self.jobs[i] for i in ids):
            if job.state in (WAITING, PAUSED, FAILED):
                self.drop_partials(job)
                self.set_job_state(job, CANCELLED)
            elif job.state == RUNNING:
                job.pausing = False
                job.stop.set()
                job.text = '中止しています…'
                self.send_job(job)

    def pause_jobs(self, ids):
        """Selected (or all) waiting / running downloads: stop for now, resume later where they were."""
        ids = [i for i in ids if i in self.jobs] or [j.id for j in self.jobs.values() if j.state in (WAITING, RUNNING)]
        for job in (self.jobs[i] for i in ids):
            if job.state == WAITING:
                self.set_job_state(job, PAUSED, progress=job.progress)
            elif job.state == RUNNING:
                job.pausing = True
                job.stop.set()
                job.text = '一時停止しています…'
                self.send_job(job)

    def resume_jobs(self, ids):
        ids = [i for i in ids if i in self.jobs] or [j.id for j in self.jobs.values() if j.state == PAUSED]
        for job in (self.jobs[i] for i in ids):
            if job.state == PAUSED:
                job.stop = threading.Event()
                self.set_job_state(job, WAITING, progress=job.progress)
        self.start_jobs()

    def retry_jobs(self, ids):
        for job in (self.jobs[i] for i in ids if i in self.jobs):
            if job.state in (FAILED, CANCELLED):
                job.stop = threading.Event()
                self.set_job_state(job, WAITING)
        self.start_jobs()

    def clear_jobs(self):
        """Take finished ones off the list; a failed one's partial files go too (confirmed by the page)."""
        gone = [j.id for j in self.jobs.values() if j.state in (DONE, FAILED, CANCELLED)]
        for i in gone:
            if self.jobs[i].state != DONE:
                self.drop_partials(self.jobs[i])
            del self.jobs[i]
        self.send('jobsRemoved', ids=gone)
        self.send_stats()
        self.save_jobs()

    def save_jobs(self):
        save_queue(QUEUE, self.jobs.values())

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
        tabs = [t.source for t in self.browsers.values() if t.source.startswith('http')]
        save_settings({'outdir': self.outdir, 'quality': self.quality, 'parallel': self.parallel,
                       'tabs': tabs if self.browsers else self.settings.get('tabs', [])})

    def on_closing(self, sender, e):
        running = sum(j.state == RUNNING for j in self.jobs.values())
        if running and MessageBox.Show(f'ダウンロード中のものが {running} 件あります。\n'
                                       '一時停止して終了し、次に起動したときに「再開」で続きから保存できます。\n\n終了しますか？',
                                       APP, MessageBoxButtons.YesNo) != DialogResult.Yes:
            e.Cancel = True
            return
        # unfinished downloads keep their partial files and come back paused next time
        for job in self.jobs.values():
            if job.state == RUNNING:
                job.pausing = True
                job.stop.set()
        self.save_jobs()
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
