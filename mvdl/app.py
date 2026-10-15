"""The window: the console on the left; tabs on the right (found videos, browser, download queue, log).

「URL から探す」 looks at a URL with yt-dlp and the page's HTML, as before. The built-in browser (WebView2)
is for pages that need a person: pass Cloudflare, log in, play the video, then 「ブラウザのページから探す」.
While browsing, the browser only reports what it loaded (capture.py); the page is not touched until the
button is pressed. WinForms + WebView2 through pythonnet; Windows only.
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
LIB = os.path.join(getattr(sys, '_MEIPASS', os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                   'mvdl', 'lib')
clr.AddReference(os.path.join(LIB, 'Microsoft.Web.WebView2.Core.dll'))
clr.AddReference(os.path.join(LIB, 'Microsoft.Web.WebView2.WinForms.dll'))

from Microsoft.Web.WebView2.Core import CoreWebView2Environment  # noqa: E402
from Microsoft.Web.WebView2.WinForms import CoreWebView2CreationProperties, WebView2  # noqa: E402
from System import Array, String  # noqa: E402
from System.Drawing import Font, Size  # noqa: E402
from System.Threading import ApartmentState, Thread, ThreadStart  # noqa: E402
from System.Windows.Forms import (  # noqa: E402
    AnchorStyles, Application, AutoScaleMode, Button, Clipboard, ColumnStyle, ComboBox, ComboBoxStyle,
    DialogResult, DockStyle, FixedPanel, FlowLayoutPanel, FolderBrowserDialog, Form, Keys, Label, LinkLabel, ListView,
    ListViewItem,
    MessageBox, MessageBoxButtons, MessageBoxIcon, Orientation, Padding, ProgressBar, ProgressBarStyle,
    RowStyle, ScrollBars, SizeType, SplitContainer, TableLayoutPanel, TabControl, TabPage, TextBox, Timer, View)

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


@dataclass
class Job:
    id: str
    cand: Candidate
    outdir: str
    quality: str
    state: str = WAITING
    stop: threading.Event = field(default_factory=threading.Event)
    files: list = field(default_factory=list)


# ---- small WinForms helpers ---------------------------------------------------------------------
def button(text, handler, width=None):
    b = Button()
    b.Text = text
    b.AutoSize = True
    if width:
        b.MinimumSize = Size(width, 0)
    b.Click += lambda s, e: handler()
    return b


def label(text=''):
    lb = Label()
    lb.Text = text
    lb.AutoSize = True
    lb.Anchor = AnchorStyles.Left
    return lb


def combo(values, value, width):
    c = ComboBox()
    c.DropDownStyle = ComboBoxStyle.DropDownList
    for v in values:
        c.Items.Add(v)
    c.SelectedItem = value if value in values else values[0]
    c.Width = width
    return c


def list_view(columns):
    lv = ListView()
    lv.View = View.Details
    lv.FullRowSelect = True
    lv.MultiSelect = True
    lv.HideSelection = False
    lv.Dock = DockStyle.Fill
    for name, width in columns:
        lv.Columns.Add(name, width)
    return lv


def row(*controls):
    p = FlowLayoutPanel()
    p.AutoSize = True
    p.Dock = DockStyle.Fill
    p.WrapContents = True
    p.Margin = Padding(0)
    for c in controls:
        p.Controls.Add(c)
    return p


def heading(text):
    lb = label(text)
    lb.Font = Font('Yu Gothic UI', 10.5)
    lb.Margin = Padding(0, 12, 0, 4)
    return lb


def tab(tabs, text, content):
    page = TabPage()
    page.Text = text
    page.Padding = Padding(4)
    content.Dock = DockStyle.Fill
    page.Controls.Add(content)
    tabs.TabPages.Add(page)
    return page


def stretch_row(left, middle, right=()):
    """[left ...][middle, stretched][right ...]"""
    t = TableLayoutPanel()
    t.Dock = DockStyle.Fill
    t.AutoSize = True
    t.Margin = Padding(0)
    t.ColumnCount = len(left) + 1 + len(right)
    for i in range(t.ColumnCount):
        t.ColumnStyles.Add(ColumnStyle(SizeType.Percent, 100) if i == len(left) else ColumnStyle(SizeType.AutoSize))
    for i, c in enumerate([*left, middle, *right]):
        if c is middle:
            c.Dock = DockStyle.Fill
        t.Controls.Add(c, i, 0)
    return t


class App:
    def __init__(self, start_url=None):
        self.q = queue.Queue()        # (kind, payload) from worker threads, drained by a UI timer
        self.capture = Capture()
        self.searching = False
        self.cands = []
        self.jobs = {}                # id -> Job, in the order they were added
        self.items = {}               # job id -> its ListViewItem
        self.job_ids = (f'j{n}' for n in itertools.count())
        self.timers = set()           # keep task-wait timers alive
        self.core = None              # CoreWebView2, once the browser is ready
        s = self.settings = load_settings()

        f = self.form = Form()
        f.Text = f'{APP} {__version__}'
        f.Font = Font('Yu Gothic UI', 9)
        f.AutoScaleMode = AutoScaleMode.Font
        f.Size = Size(1500, 920)
        f.MinimumSize = Size(1000, 640)

        # ---- left: the console
        self.url = TextBox()
        self.url.KeyDown += self.on_url_key
        self.find_btn = button('URL から探す', self.on_find_url, 150)
        self.open_btn = button('ブラウザで開く', self.on_open_in_browser, 150)
        self.page_btn = button('ブラウザのページから探す', self.on_find_page, 306)
        self.captured_lbl = LinkLabel()  # click: show them
        self.captured_lbl.AutoSize = True
        self.captured_lbl.LinkClicked += lambda s, e: setattr(self.tabs, 'SelectedTab', self.tab_captured)
        self.bar = ProgressBar()
        self.bar.Style = ProgressBarStyle.Marquee
        self.bar.MarqueeAnimationSpeed = 0
        self.bar.Dock = DockStyle.Fill
        self.status = label('URL を入れて「URL から探す」を押してください')
        self.status.MaximumSize = Size(330, 0)
        self.outdir = TextBox()
        self.outdir.Text = s.get('outdir') or default_outdir()
        self.quality = combo(list(QUALITIES), s.get('quality'), 110)
        self.parallel = combo(PARALLEL, str(s.get('parallel', 2)), 45)
        self.queue_lbl = label()

        console = TableLayoutPanel()
        console.Dock = DockStyle.Fill
        console.Padding = Padding(8)
        console.AutoScroll = True
        rows = [
            heading('動画を探す'),
            label('ページの URL'),
            self.url,
            row(self.find_btn, self.open_btn),
            row(self.page_btn),
            self.captured_lbl,
            self.bar,
            self.status,
            heading('ダウンロード'),
            stretch_row([label('保存先')], self.outdir, [button('参照…', self.on_browse)]),
            row(label('画質'), self.quality),
            row(button('選んだ動画をダウンロードに追加', self.on_enqueue, 306)),
            heading('ダウンロード一覧'),
            self.queue_lbl,
            row(label('同時に'), self.parallel, label('件ずつ保存')),
            row(button('中止', self.on_cancel_jobs), button('再試行', self.on_retry),
                button('完了・失敗を消す', self.on_clear)),
            row(button('保存先を開く', self.on_open_folder)),
        ]
        console.RowCount = len(rows) + 1
        for i, ctrl in enumerate(rows):
            console.RowStyles.Add(RowStyle(SizeType.AutoSize))
            if isinstance(ctrl, (TextBox, ProgressBar)):
                ctrl.Dock = DockStyle.Fill
            console.Controls.Add(ctrl, 0, i)
        console.RowStyles.Add(RowStyle(SizeType.Percent, 100))

        # ---- right: tabs
        self.found = list_view([('見つかった動画', 520), ('種類', 80), ('解像度', 100), ('長さ', 70),
                                ('サイズ', 95), ('見つけ方', 80)])
        self.found.DoubleClick += lambda s, e: self.on_enqueue()

        self.address = TextBox()
        self.address.KeyDown += self.on_address_key
        nav = stretch_row([button('←', lambda: self.core and self.core.GoBack()),
                           button('→', lambda: self.core and self.core.GoForward()),
                           button('⟳', lambda: self.core and self.core.Reload())],
                          self.address, [button('移動', lambda: self.navigate(self.address.Text)),
                                         button('このページから探す', self.on_find_page)])
        self.web = WebView2()
        self.web.Dock = DockStyle.Fill
        props = CoreWebView2CreationProperties()
        props.UserDataFolder = os.path.join(DATA, 'webview2')  # cookies / logins / passed checks persist
        self.web.CreationProperties = props
        self.web.CoreWebView2InitializationCompleted += self.on_web_ready
        browser = TableLayoutPanel()
        browser.Dock = DockStyle.Fill
        browser.RowCount = 2
        browser.RowStyles.Add(RowStyle(SizeType.AutoSize))
        browser.RowStyles.Add(RowStyle(SizeType.Percent, 100))
        browser.Controls.Add(nav, 0, 0)
        browser.Controls.Add(self.web, 0, 1)

        # everything the browser received as a video, unchecked, as it comes in
        self.captured = list_view([('時刻', 70), ('種類', 70), ('Content-Type', 170), ('URL', 700)])
        self.captured.DoubleClick += lambda s, e: self.on_enqueue()

        self.queue = list_view([('タイトル', 520), ('画質', 90), ('サイズ', 95), ('状態', 300)])
        self.queue.DoubleClick += lambda s, e: self.on_open_folder()

        self.log_box = TextBox()
        self.log_box.Multiline = True
        self.log_box.ReadOnly = True
        self.log_box.ScrollBars = ScrollBars.Vertical
        self.log_box.Dock = DockStyle.Fill

        self.tabs = TabControl()
        self.tabs.Dock = DockStyle.Fill
        self.tab_found = tab(self.tabs, '見つかった動画', self.found)
        self.tab_browser = tab(self.tabs, 'ブラウザ', browser)
        self.tab_captured = tab(self.tabs, 'ブラウザで読み込んだ動画', self.captured)
        self.tab_queue = tab(self.tabs, 'ダウンロード一覧', self.queue)
        self.tab_log = tab(self.tabs, 'ログ', self.log_box)

        split = self.split = SplitContainer()
        split.Dock = DockStyle.Fill
        split.Orientation = Orientation.Vertical
        split.FixedPanel = FixedPanel.Panel1
        split.Panel1.Controls.Add(console)
        split.Panel2.Controls.Add(self.tabs)
        f.Controls.Add(split)

        f.Load += lambda s, e: self.on_load(start_url)
        f.FormClosing += self.on_closing
        self.ticker = Timer()
        self.ticker.Interval = 100
        self.ticker.Tick += lambda s, e: self.poll()
        self.ticker.Start()

    # ---- browser ----------------------------------------------------------------------------------
    def on_load(self, start_url):
        self.split.SplitterDistance = 360
        self.update_captured_label()
        self.update_queue_label()
        if not find_tool('ffmpeg'):
            self.log('ffmpeg が見つかりません。exe と同じフォルダに ffmpeg.exe を置くと、高画質の動画も保存できます')
        try:
            CoreWebView2Environment.SetLoaderDllFolderPath(LIB)
            self.web.EnsureCoreWebView2Async(None)
        except Exception as e:  # noqa: BLE001
            self.browser_missing(e)
        self.url.Text = start_url or self.clipboard_url() or ''
        self.pending_url = self.settings.get('last_url') or ''

    def browser_missing(self, err):
        self.log(f'ブラウザ（WebView2）を開けませんでした: {err}')
        MessageBox.Show(f'ブラウザの部品（Microsoft Edge WebView2 ランタイム）を使えませんでした。\n\n'
                        f'{err}\n\nWebView2 ランタイムをインストールしてから起動し直してください:\n{WEBVIEW2_RUNTIME}',
                        APP, MessageBoxButtons.OK, MessageBoxIcon.Error)

    def on_web_ready(self, sender, e):
        if not e.IsSuccess:
            self.browser_missing(e.InitializationException)
            return
        core = self.core = self.web.CoreWebView2
        core.WebResourceResponseReceived += self.on_response
        core.NavigationStarting += self.on_navigation
        core.SourceChanged += lambda s, a: setattr(self.address, 'Text', core.Source)
        core.DocumentTitleChanged += lambda s, a: setattr(self.form, 'Text', f'{core.DocumentTitle} - {APP}')
        core.NewWindowRequested += self.on_new_window
        self.log(f'ブラウザ: WebView2 {core.Environment.BrowserVersionString}')
        if os.environ.get('MVDL_STARTUP_CHECK'):  # build check: the exe starts and its browser comes up
            with open(os.environ['MVDL_STARTUP_CHECK'], 'w', encoding='utf-8') as fh:
                fh.write(core.Environment.BrowserVersionString)
            self.form.Close()
            return
        if self.pending_url:
            self.navigate(self.pending_url)

    def on_navigation(self, sender, e):
        if not e.IsRedirected:  # a new page: what the previous one loaded no longer counts
            self.capture.reset()
            self.captured.Items.Clear()
            self.update_captured_label()

    def on_response(self, sender, e):
        """Every response the browser receives (all frames). Cheap unless it is a video."""
        uri = e.Request.Uri
        mime = e.Response.Headers.GetHeader('Content-Type') if e.Response.Headers.Contains('Content-Type') else ''
        if not is_media(uri, mime):
            return
        m = self.capture.add(uri, mime, {kv.Key: kv.Value for kv in e.Request.Headers})
        if m:
            kind = kind_of(m.url)
            item = ListViewItem(Array[String]([time.strftime('%H:%M:%S', time.localtime(m.at)),
                                               'mp4' if kind == '動画' else kind, m.mime or '—', m.url]))
            item.Name = m.url
            self.captured.Items.Add(item)
            self.update_captured_label()

    def update_captured_label(self):
        self.captured_lbl.Text = f'ブラウザのページで読み込んだ動画: {len(self.capture)} 件（クリックで表示）'

    def on_new_window(self, sender, e):
        """Links that open a new tab stay in this browser (so their videos are seen). Sized pop-ups
        (sign-in windows) open as pop-ups."""
        if not e.WindowFeatures.HasSize:
            e.Handled = True
            self.core.Navigate(e.Uri)

    def navigate(self, url):
        url = normalized(url)
        if not url:
            return
        self.address.Text = url
        if self.core:
            try:
                self.core.Navigate(url)
            except Exception as e:  # noqa: BLE001 - bad URL
                self.log(f'開けませんでした: {url} ({e})')
        else:
            self.pending_url = url

    def on_open_in_browser(self):
        url = normalized(self.url.Text)
        if url:
            self.navigate(url)
            self.tabs.SelectedTab = self.tab_browser

    def on_url_key(self, sender, e):
        if e.KeyCode == Keys.Enter:
            e.SuppressKeyPress = True
            self.on_find_url()

    def on_address_key(self, sender, e):
        if e.KeyCode == Keys.Enter:
            e.SuppressKeyPress = True
            self.navigate(self.address.Text)

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

    # ---- search -----------------------------------------------------------------------------------
    def on_find_url(self):
        """yt-dlp and the page's HTML, fetched by us (no browser)."""
        url = normalized(self.url.Text)
        if self.searching or not url:
            return
        self.url.Text = url
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

    def on_blocked(self, url):
        self.set_searching(False)
        self.status.Text = 'Cloudflare の確認画面（ボット対策）で止められました'
        if MessageBox.Show('Cloudflare の確認画面（ボット対策）で止められました。\n\n'
                           'ブラウザのタブで開きます。確認を通って動画を再生したら、'
                           '「ブラウザのページから探す」を押してください。\n\nブラウザで開きますか？',
                           APP, MessageBoxButtons.YesNo) == DialogResult.Yes:
            self.navigate(url)
            self.tabs.SelectedTab = self.tab_browser

    def on_find_page(self):
        """The page open in the browser tab: what it loaded, its DOM now, and yt-dlp with its cookies."""
        if self.searching:
            return
        core = self.core
        page_url = core.Source if core else ''
        if not page_url.startswith('http'):
            self.status.Text = '「ブラウザ」タブでページを開いてから押してください'
            self.tabs.SelectedTab = self.tab_browser
            return
        title, ua = core.DocumentTitle, core.Settings.UserAgent
        self.set_searching(True)
        self.log(f'--- {page_url}（ブラウザ）')
        # the page as it is now (after JavaScript), and the cookies the browser holds
        html_task = core.ExecuteScriptAsync('document.documentElement.outerHTML')
        urls = [page_url, *list(self.capture.media)[:40]]
        cookie_tasks = {u: core.CookieManager.GetCookiesAsync(u) for u in urls}

        def ready():
            html = ''
            if not html_task.IsFaulted:
                try:
                    html = json.loads(html_task.Result) or ''
                except (ValueError, TypeError):
                    pass
            cookies = {}
            for u, t in cookie_tasks.items():
                if not t.IsFaulted and t.Result is not None:
                    cookies[u] = '; '.join(f'{c.Name}={c.Value}' for c in t.Result)
            captured = self.capture.candidates(page_url, title, ua, lambda u: cookies.get(u))
            headers = {'User-Agent': ua} | ({'Cookie': cookies[page_url]} if cookies.get(page_url) else {})
            log = lambda m: self.q.put(('log', m))  # noqa: E731

            def work():
                try:
                    self.q.put(('found', find_in_page(page_url, title, html, captured, headers, log)))
                except Exception as e:  # noqa: BLE001 - shown to the user
                    self.q.put(('log', f'エラー: {e}'))
                    self.q.put(('found', []))

            threading.Thread(target=work, daemon=True).start()

        self.when_done([html_task, *cookie_tasks.values()], ready)

    def set_searching(self, on):
        self.searching = on
        for b in (self.find_btn, self.page_btn):
            b.Enabled = not on
        self.bar.MarqueeAnimationSpeed = 30 if on else 0
        if on:
            self.found.Items.Clear()
            self.status.Text = '探しています…'

    def show(self, cands):
        self.set_searching(False)
        self.cands = cands
        self.found.Items.Clear()
        for i, c in enumerate(cands):
            label_ = c.title if c.source == 'yt-dlp' else f'{c.title} — {c.url}'
            item = ListViewItem(Array[String]([label_, c.kind, c.resolution or '—', fmt_duration(c.duration),
                                               fmt_size(c.size, c.size_approx), c.source]))
            item.Name = str(i)
            self.found.Items.Add(item)
        self.tabs.SelectedTab = self.tab_found
        if cands:
            self.found.Items[0].Selected = True
            self.status.Text = f'{len(cands)} 件見つかりました。選んで「ダウンロードに追加」を押してください'
        else:
            self.status.Text = ('動画が見つかりませんでした。「ブラウザで開く」で動画を再生してから、'
                                '「ブラウザのページから探す」を試してください')

    # ---- worker messages ----------------------------------------------------------------------------
    def log(self, msg):
        self.log_box.AppendText(msg + '\r\n')

    def poll(self):
        try:
            while True:
                kind, v = self.q.get_nowait()
                if kind == 'log':
                    self.log(v)
                elif kind == 'found':
                    self.show(v)
                elif kind == 'blocked':
                    self.on_blocked(v)
                elif kind == 'job':
                    job_id, text = v
                    self.set_job_state(self.jobs[job_id], text=text)
                elif kind == 'job-end':
                    job_id, state, text = v
                    self.set_job_state(self.jobs[job_id], state, text)
        except queue.Empty:
            pass
        self.start_jobs()

    # ---- download queue -----------------------------------------------------------------------------
    def set_job_state(self, job, state=None, text=None):
        if state:
            job.state = state
        item = self.items.get(job.id)
        if item is not None:
            item.SubItems[3].Text = text or job.state
        self.update_queue_label()

    def update_queue_label(self):
        states = [j.state for j in self.jobs.values()]
        parts = [f'{s} {states.count(s)}' for s in (RUNNING, WAITING, DONE, FAILED) if states.count(s)]
        self.queue_lbl.Text = '（' + '、'.join(parts) + '）' if parts else ''

    def start_jobs(self):
        """Start waiting jobs, oldest first, up to the chosen number at once."""
        running = sum(j.state == RUNNING for j in self.jobs.values())
        for job in self.jobs.values():
            if running >= int(str(self.parallel.SelectedItem)):
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

    def selected(self, lv):
        return [item.Name for item in lv.SelectedItems]

    def on_enqueue(self):
        """The selection of the tab in view: 「ブラウザで読み込んだ動画」, else 「見つかった動画」."""
        if self.tabs.SelectedTab == self.tab_captured:
            self.enqueue_captured()
            return
        sel = [self.cands[int(i)] for i in self.selected(self.found)]
        if not sel:
            self.tabs.SelectedTab = self.tab_found
            MessageBox.Show('ダウンロードする動画を「見つかった動画」か「ブラウザで読み込んだ動画」から選んでください', APP)
            return
        self.add_jobs(sel)

    def enqueue_captured(self):
        """As the browser loaded them (no check), with the cookies it sent or holds."""
        urls = self.selected(self.captured)
        if not urls:
            MessageBox.Show('ダウンロードする動画を一覧から選んでください', APP)
            return
        core = self.core
        page_url, title, ua = core.Source, core.DocumentTitle, core.Settings.UserAgent
        tasks = {u: core.CookieManager.GetCookiesAsync(u) for u in urls}

        def ready():
            cookies = {u: '; '.join(f'{c.Name}={c.Value}' for c in t.Result)
                       for u, t in tasks.items() if not t.IsFaulted and t.Result is not None}
            cands = self.capture.candidates(page_url, title, ua, cookies.get, set(urls))
            for c in cands:
                c.source = 'ブラウザ（未確認）'
            self.add_jobs(cands)

        self.when_done(list(tasks.values()), ready)

    def add_jobs(self, sel):
        outdir, quality = self.outdir.Text.strip() or default_outdir(), str(self.quality.SelectedItem)
        active = {(j.cand.url, j.quality) for j in self.jobs.values() if j.state in (WAITING, RUNNING)}
        added = 0
        for c in sel:
            if (c.url, quality) in active:
                self.log(f'追加済みです: {c.title}')
                continue
            job = Job(next(self.job_ids), c, outdir, quality)
            self.jobs[job.id] = job
            item = ListViewItem(Array[String]([c.title, quality, fmt_size(c.size, c.size_approx), WAITING]))
            item.Name = job.id
            self.items[job.id] = item
            self.queue.Items.Add(item)
            added += 1
        if added:
            self.status.Text = f'{added} 件をダウンロード一覧に追加しました（「ダウンロード一覧」タブで進み具合を見られます）'
        self.save()
        self.update_queue_label()
        self.start_jobs()

    def on_cancel_jobs(self):
        ids = self.selected(self.queue) or [j.id for j in self.jobs.values() if j.state in (WAITING, RUNNING)]
        for job in (self.jobs[i] for i in ids):
            if job.state == WAITING:
                self.set_job_state(job, CANCELLED)
            elif job.state == RUNNING:
                job.stop.set()
                self.set_job_state(job, text='中止しています…')

    def on_retry(self):
        for job in (self.jobs[i] for i in self.selected(self.queue)):
            if job.state in (FAILED, CANCELLED):
                job.stop = threading.Event()
                self.set_job_state(job, WAITING)
        self.start_jobs()

    def on_clear(self):
        for job in list(self.jobs.values()):
            if job.state in (DONE, FAILED, CANCELLED):
                del self.jobs[job.id]
                self.queue.Items.Remove(self.items.pop(job.id))
        self.update_queue_label()

    def on_browse(self):
        dlg = FolderBrowserDialog()
        dlg.SelectedPath = self.outdir.Text or default_outdir()
        if dlg.ShowDialog(self.form) == DialogResult.OK:
            self.outdir.Text = dlg.SelectedPath

    def on_open_folder(self):
        ids = self.selected(self.queue)
        d = self.jobs[ids[0]].outdir if ids else (self.outdir.Text.strip() or default_outdir())
        os.makedirs(d, exist_ok=True)
        os.startfile(d)  # noqa: S606

    def save(self):
        save_settings({'outdir': self.outdir.Text.strip(), 'quality': str(self.quality.SelectedItem),
                       'parallel': int(str(self.parallel.SelectedItem)),
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
