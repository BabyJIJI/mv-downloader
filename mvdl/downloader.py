"""Download one Candidate with yt-dlp."""
import glob
import os

import yt_dlp
from yt_dlp.utils import sanitize_filename

from .finder import Candidate, Logger, fmt_size
from .jobs import partial_files, remove_partials
from .tools import find_tool, ydl_base_opts

QUALITIES = {
    '最高画質': 'bv*+ba/b',
    '1080p まで': 'bv*[height<=1080]+ba/b[height<=1080]/b',
    '720p まで': 'bv*[height<=720]+ba/b[height<=720]/b',
    '480p まで': 'bv*[height<=480]+ba/b[height<=480]/b',
    '音声のみ': 'ba[ext=m4a]/ba/b',
}
# without ffmpeg, video and audio cannot be merged: best file that already has both
SINGLE_FILE = {
    '最高画質': 'b',
    '1080p まで': 'b[height<=1080]/b',
    '720p まで': 'b[height<=720]/b',
    '480p まで': 'b[height<=480]/b',
    '音声のみ': 'ba[ext=m4a]/ba/b',
}


class Cancelled(Exception):
    pass


class Incomplete(Exception):
    """yt-dlp returned without an error, but the file is not there (only partial files, or nothing)."""


def container_of(path):
    """The real container of a file, from its first bytes: 'mp4', 'webm', ... or None."""
    try:
        with open(path, 'rb') as f:
            head = f.read(4096)
    except OSError:
        return None
    if head[4:8] == b'ftyp':
        brand = head[8:12]
        return 'mov' if brand == b'qt  ' else 'm4a' if brand in (b'M4A ', b'M4B ') else 'mp4'
    if head[4:8] in (b'moov', b'mdat', b'wide', b'free'):
        return 'mov'
    if head[:4] == b'\x1aE\xdf\xa3':  # EBML
        return 'webm' if b'webm' in head[:64] else 'mkv'
    if head[:3] == b'FLV':
        return 'flv'
    if head[:4] == b'RIFF' and head[8:12] == b'AVI ':
        return 'avi'
    if len(head) > 376 and head[0] == head[188] == head[376] == 0x47:  # MPEG-TS sync bytes
        return 'ts'
    return None


def fix_extension(path, log):
    """yt-dlp names a file "*.unknown_video" when neither the URL nor the Content-Type (octet-stream, or
    none) tells the format; the file itself is fine. Give it the extension of what it really is."""
    root, ext = os.path.splitext(path)
    if ext.lower() not in ('.unknown_video', '') or not os.path.isfile(path):
        return path
    real = container_of(path)
    if not real:
        log(f'形式が分からないため、拡張子を付けられませんでした: {os.path.basename(path)}')
        return path
    new, n = f'{root}.{real}', 2
    while os.path.exists(new):
        new, n = f'{root} ({n}).{real}', n + 1
    os.replace(path, new)
    log(f'拡張子を付けました: {os.path.basename(new)}')
    return new


def download(cand: Candidate, outdir, quality, log, progress=None, stop=None, on_file=None):
    """progress(fraction or None, text); on_file(name) for each file yt-dlp writes (its partials are
    name.part etc.). Returns the saved file paths."""
    quality = quality if quality in QUALITIES else '最高画質'
    fmt = QUALITIES[quality]
    if not find_tool('ffmpeg'):
        fmt = SINGLE_FILE[quality]
        log('ffmpeg が無いため、映像と音声が一体のファイルだけを選びます（画質が下がることがあります）')

    if cand.source == 'yt-dlp':
        name = '%(title).150B [%(id)s].%(ext)s'
    else:  # direct media URL: name it after the page instead of the file on the CDN
        name = sanitize_filename(cand.title, restricted=False)[:150].replace('%', '%%') + '.%(ext)s'

    files, seen = [], []  # finished files; every file name yt-dlp worked on

    def hook(d):
        if d.get('filename') and d['filename'] not in seen:
            seen.append(d['filename'])
        if on_file and d.get('filename'):
            on_file(d['filename'])
        if stop is not None and stop.is_set():
            raise Cancelled()
        if not progress:
            return
        if d['status'] == 'downloading':
            done, total = d.get('downloaded_bytes') or 0, d.get('total_bytes') or d.get('total_bytes_estimate')
            frac = done / total if total else None
            if d.get('fragment_count'):
                frac = (d.get('fragment_index') or 0) / d['fragment_count']
            text = fmt_size(done) + (f' / {fmt_size(total)}' if total else '')
            if d.get('speed'):
                text += f'  {fmt_size(d["speed"])}/s'
            if d.get('eta') is not None:
                text += f'  残り {int(d["eta"])} 秒'
            progress(frac, text)
        elif d['status'] == 'finished':
            progress(1.0, '仕上げ中…')

    def pp_hook(d):
        if d['status'] == 'finished' and d.get('info_dict', {}).get('filepath'):
            files.append(d['info_dict']['filepath'])

    opts = ydl_base_opts(Logger(log)) | {
        'format': fmt,
        'outtmpl': {'default': os.path.join(outdir, name)},
        'paths': {'home': outdir},
        'noplaylist': True,
        'merge_output_format': 'mp4',
        'concurrent_fragment_downloads': 4,
        'progress_hooks': [hook],
        'postprocessor_hooks': [pp_hook],
        'overwrites': False,
    }
    if cand.headers:
        opts['http_headers'] = dict(cand.headers)
    os.makedirs(outdir, exist_ok=True)

    def run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(cand.url, download=True)

    def our_partials():
        """The partial files of this download. The 416 comes on the first request of the resume, before
        any progress names the file, so ask yt-dlp for the name (no download)."""
        names = list(seen)
        with yt_dlp.YoutubeDL(opts | {'progress_hooks': [], 'postprocessor_hooks': []}) as ydl:
            base = os.path.splitext(ydl.prepare_filename(ydl.extract_info(cand.url, download=False)))[0]
        # the file itself, and the single formats of a merged download ("Title.f137.mp4")
        names += glob.glob(glob.escape(base) + '.*.part') + glob.glob(glob.escape(base) + '.*.ytdl')
        return [n[:-5] if n.endswith(('.part', '.ytdl')) else n for n in names]

    try:
        try:
            info = run()
        except yt_dlp.utils.DownloadError as e:
            if 'HTTP Error 416' not in str(e):
                raise
            names = our_partials()
            if not partial_files(names):
                raise
            # resuming a partial file, but the server refuses ranges: start over (once)
            log('サーバーが続きからの取得に応じないため、途中のファイルを消して最初から取り直します')
            remove_partials(names)
            info = run()
    except Exception:
        if stop is not None and stop.is_set():
            raise Cancelled() from None
        raise
    if not files and info:
        for d in info.get('requested_downloads') or [info]:
            if d.get('filepath'):
                files.append(d['filepath'])
    files = [p for p in dict.fromkeys(files) if os.path.isfile(p) and os.path.getsize(p) > 0]
    if not files:
        left = partial_files(seen)
        raise Incomplete('保存が完了していません'
                         + (f'（途中のファイルだけが残っています: {", ".join(os.path.basename(p) for p in left)}）'
                            if left else '（ファイルが作られませんでした）'))
    return [fix_extension(p, log) for p in files]

