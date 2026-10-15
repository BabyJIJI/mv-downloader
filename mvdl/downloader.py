"""Download one Candidate with yt-dlp."""
import os

import yt_dlp
from yt_dlp.utils import sanitize_filename

from .finder import Candidate, Logger, fmt_size
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

    files = []

    def hook(d):
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
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(cand.url, download=True)
    except Exception:
        if stop is not None and stop.is_set():
            raise Cancelled() from None
        raise
    if not files and info:
        for d in info.get('requested_downloads') or [info]:
            if d.get('filepath'):
                files.append(d['filepath'])
    return list(dict.fromkeys(files))

