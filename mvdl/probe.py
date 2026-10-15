"""Keep only real videos among the URLs found in the HTML or in the browser traffic.

URLs are picked by their extension or MIME type, which also catches audio, images, subtitle playlists,
HTML player pages and the per-quality / audio playlists inside an HLS master. Each one is checked:
ffprobe (when bundled) reads the streams; otherwise the Content-Type decides. HLS playlists are read
as text.
"""
import json
import os
import re
import subprocess
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import yt_dlp
from yt_dlp.networking import Request

from .finder import Logger, fmt_resolution
from .tools import find_tool, ydl_base_opts

VIDEO_CODECS = ('avc1', 'avc3', 'hvc1', 'hev1', 'vp08', 'vp8', 'vp09', 'vp9', 'av01', 'mp4v', 'dvh1', 'dvhe')
NOT_VIDEO_EXT = ('aac', 'mp3', 'm4a', 'opus', 'ogg', 'vtt', 'webvtt', 'srt', 'jpg', 'jpeg', 'png', 'webp', 'gif')


def _get(url, headers, limit, log, rng=False):
    """(content-type, first `limit` bytes, total size or None)."""
    h = dict(headers)
    if rng:
        h['Range'] = f'bytes=0-{limit - 1}'
    with yt_dlp.YoutubeDL(ydl_base_opts(Logger(log))) as ydl:
        with ydl.urlopen(Request(url, headers=h)) as r:
            ctype = (r.headers.get('Content-Type') or '').split(';')[0].strip().lower()
            m = re.search(r'/(\d+)$', r.headers.get('Content-Range') or '')
            total = int(m.group(1)) if m else None
            if total is None and r.status == 200 and (r.headers.get('Content-Length') or '').isdigit():
                total = int(r.headers['Content-Length'])
            return ctype, r.read(limit), total


def _ffprobe(url, headers):
    """dict(width, height, duration, size, bit_rate) when the URL has a video stream, else None.
    Raises on probe errors."""
    ffprobe = find_tool('ffprobe')
    h = ''.join(f'{k}: {v}\r\n' for k, v in headers.items() if k.lower() != 'user-agent')
    cmd = [ffprobe, '-v', 'error', '-rw_timeout', '15000000', '-of', 'json', '-show_streams', '-show_format']
    if headers.get('User-Agent'):
        cmd += ['-user_agent', headers['User-Agent']]
    if h:
        cmd += ['-headers', h]
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    r = subprocess.run(cmd + [url], capture_output=True, timeout=40, creationflags=flags)
    if r.returncode:
        raise RuntimeError(r.stderr.decode('utf-8', 'replace').strip().splitlines()[-1:] or 'ffprobe failed')
    data = json.loads(r.stdout or b'{}')
    videos = [s for s in data.get('streams', [])
              if s.get('codec_type') == 'video' and not (s.get('disposition') or {}).get('attached_pic')
              and s.get('codec_name') not in ('mjpeg', 'png', 'bmp', 'gif', 'webp')]
    if not videos:
        return None
    fmt = data.get('format', {})
    num = lambda k: float(fmt[k]) if fmt.get(k) not in (None, 'N/A') else None  # noqa: E731
    best = max(videos, key=lambda s: s.get('height') or 0)
    return {'width': best.get('width'), 'height': best.get('height'), 'duration': num('duration'),
            'size': int(num('size')) if num('size') else None, 'bit_rate': num('bit_rate')}


def _hls_attrs(line):
    return dict(re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line.split(':', 1)[1]))


def _check_hls(c, log):
    """(info or None, URLs of the playlists this master contains)."""
    _ctype, body, _total = _get(c.url, c.headers, 2 * 1024 * 1024, log)
    text = body.decode('utf-8', 'replace')
    if not text.lstrip('﻿ \r\n').startswith('#EXTM3U'):
        return None, set()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if any(ln.startswith('#EXT-X-STREAM-INF') for ln in lines):  # master playlist
        children, sizes, video, best = set(), [], False, (0, None)  # best: (bandwidth, playlist URL)
        for i, ln in enumerate(lines):
            if ln.startswith('#EXT-X-STREAM-INF'):
                a = _hls_attrs(ln)
                codecs = a.get('CODECS', '').strip('"').lower()
                if 'RESOLUTION' in a or not codecs or any(v in codecs for v in VIDEO_CODECS):
                    video = True
                if 'RESOLUTION' in a:
                    w, _, h = a['RESOLUTION'].lower().partition('x')
                    sizes.append((int(h), int(w)))
                nxt = next((x for x in lines[i + 1:] if not x.startswith('#')), None)
                if nxt:
                    children.add(urllib.parse.urljoin(c.url, nxt))
                    bw = int(a.get('BANDWIDTH', '0').strip('"') or 0)
                    if bw > best[0]:
                        best = (bw, urllib.parse.urljoin(c.url, nxt))
            elif ln.startswith(('#EXT-X-MEDIA', '#EXT-X-I-FRAME-STREAM-INF')):
                uri = _hls_attrs(ln).get('URI')
                if uri:
                    children.add(urllib.parse.urljoin(c.url, uri.strip('"')))
        if not video:
            return None, children
        h, w = max(sizes) if sizes else (None, None)
        info = {'width': w, 'height': h}
        if best[1]:  # size: the best variant's bandwidth x its duration
            try:
                _, variant, _ = _get(best[1], c.headers, 2 * 1024 * 1024, log)
                duration = _hls_duration(variant.decode('utf-8', 'replace'))
                if duration:
                    info |= {'duration': duration, 'size': int(best[0] * duration / 8), 'size_approx': True}
            except Exception:  # noqa: BLE001 - size is optional
                pass
        return info, children
    # media playlist: look at the first segment
    seg = next((ln for ln in lines if not ln.startswith('#')), None)
    if not seg:
        return None, set()
    ext = urllib.parse.urlparse(seg).path.rsplit('.', 1)[-1].lower()
    if ext in NOT_VIDEO_EXT:
        return None, set()
    duration = _hls_duration(text)
    if find_tool('ffprobe'):
        info = _ffprobe(urllib.parse.urljoin(c.url, seg), c.headers)
        if info is None:
            return None, set()
        size = info['bit_rate'] * duration / 8 if info.get('bit_rate') and duration else None
        return {'width': info['width'], 'height': info['height'], 'duration': duration, 'size': size and int(size),
                'size_approx': True}, set()
    return {'duration': duration}, set()


def _hls_duration(text):
    return sum(float(m) for m in re.findall(r'#EXTINF:([\d.]+)', text)) or None


def _check_file(c, log):
    if find_tool('ffprobe'):
        return _ffprobe(c.url, c.headers)
    ctype, body, total = _get(c.url, c.headers, 4096, log, rng=True)
    info = {'size': total if ctype != 'application/dash+xml' else None}
    if ctype.startswith('video/') or ctype == 'application/dash+xml':
        return info
    if ctype in ('', 'application/octet-stream', 'binary/octet-stream'):
        # unknown type: an MP4 / WebM header still says it's a video container
        return info if body[4:8] in (b'ftyp', b'moov') or body[:4] == b'\x1aE\xdf\xa3' else None
    return None


def check(c, log):
    """(Candidate with resolution / duration / size filled in, or None if it is not a video;
    contained playlist URLs)."""
    try:
        if c.kind == 'HLS':
            info, children = _check_hls(c, log)
        else:
            info, children = _check_file(c, log), set()
    except Exception as e:  # noqa: BLE001 - unreachable or unreadable
        reason = str(e).replace('ERROR: ', '')[:200]
        if c.source == 'ブラウザ':
            # the browser itself received it as a video; only our own request was refused (bot check,
            # one-time URL, ...), so keep it and let the user decide
            c.source = 'ブラウザ（未確認）'
            log(f'中身を確かめられませんでしたが、ブラウザが動画として受け取ったので残します: {c.url}（{reason}）')
            return c, set()
        log(f'確認できないので除きました: {c.url}（{reason}）')
        return None, set()
    if info is None:
        log(f'動画ではないので除きました: {c.url}')
        return None, children
    c.resolution, c.duration = fmt_resolution(info), info.get('duration')
    c.size, c.size_approx = info.get('size'), bool(info.get('size_approx'))
    return c, children


def keep_videos(cands, log):
    if not cands:
        return []
    log(f'{len(cands)} 件の URL が動画かどうかを確かめています…')
    with ThreadPoolExecutor(6) as ex:
        results = list(ex.map(lambda c: check(c, log), cands))
    inner = set().union(*(children for _, children in results))
    return [c for c, _ in results if c and c.url not in inner]
