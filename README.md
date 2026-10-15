# mv-downloader（動画ダウンローダー）

Web ページに埋め込まれた動画を保存する Windows 用の GUI ツール。

1. ページの URL を貼り付けて「動画を探す」
2. 見つかった動画を一覧から選ぶ（複数選択可）。一覧には種類・解像度・長さ・ファイルサイズが出る
3. 画質と保存先を選んで「ダウンロードに追加」。下のダウンロード一覧に積まれ、順に保存される

ダウンロード中も、別のページを探して追加できる。同時に保存する数は 1〜4 で選べる（初期値 2）。ダウンロード一覧で選んだものの中止・再試行、完了・失敗したものの消去ができる。

一覧に出すのは映像の入ったものだけ。ページ内やブラウザの通信で見つけた URL は、ffprobe（無ければ Content-Type）で中身を確かめ、音声だけ・画像・字幕・HTML などは除く。HLS は親のプレイリストだけを出し、その中の画質別・音声のプレイリストはまとめる。HLS のサイズは「ビットレート × 長さ」の見積もり（「約」が付く）。

## 動画の見つけ方

上から順に試し、見つかった時点で止める。

| 順番 | 方法 | 向いているページ |
|---|---|---|
| 1 | [yt-dlp](https://github.com/yt-dlp/yt-dlp) | YouTube・Vimeo・X など、yt-dlp が対応する約 1,800 サイト。HTML に `<video>` があるページ |
| 2 | ページの HTML を読む | `<video>` / `<source>`、`og:video`、スクリプト内の `.mp4` `.m3u8` `.mpd` の URL、YouTube などの埋め込み iframe |
| 3 | Edge で再生して通信を見る | JavaScript でプレーヤーを読み込むページ。ヘッドレスの Edge でページを開き、動画をミュート再生して、動画や HLS/DASH マニフェストの URL を拾う。Cookie と Referer も引き継ぐ |

3 は画面の「見つからないときは Edge で再生して探す」で切り替えられる（20 秒ほどかかる）。

### Cloudflare の確認画面で止められるとき・ログインが必要なとき

「ブラウザで開いて探す」を押すと、Edge が画面に出る。確認（「人間であることを確認します」など）やログインを自分で済ませ、動画を再生して「完了」を押す（またはブラウザを閉じる）と、その間に読み込まれた動画が一覧に出る。ダウンロードには、そのブラウザが実際に送った Cookie と User-Agent を使う。

「動画を探す」で Cloudflare に止められたときは、ブラウザを開くか聞かれる。

このブラウザのプロファイルは `%LOCALAPPDATA%\mvdl\browser` に残るので、確認やログインは次回以降も引き継がれる（普段使っている Edge とは別）。

## 使い方（配布版）

[Releases](../../releases) または Actions の成果物から `mvdl-win64.zip` を取り、展開して `mvdl.exe` を実行する。インストールは不要。

| ファイル | 役割 |
|---|---|
| `mvdl.exe` | 本体 |
| `ffmpeg.exe` / `ffprobe.exe` | 映像と音声の結合、HLS/DASH の結合。無くても動くが、映像と音声が一体のファイルしか選べず、画質が下がることがある |
| `deno.exe` | YouTube の署名の解読に yt-dlp が使う |

設定（保存先・画質）は `%APPDATA%\mvdl\settings.json` に保存される。

## ビルド

GitHub に push すると Actions（`.github/workflows/build.yml`）がテストし、Windows で exe を作る。`v*` のタグを push すると Release に zip を載せる。

```bash
git tag v0.1.0 && git push origin v0.1.0
```

手元の Windows で作るとき（Python 3.11 以上が必要。`winget install Python.Python.3.12`）：

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1          # dist\mvdl-win64.zip
powershell -ExecutionPolicy Bypass -File build.ps1 -NoTools # exe だけ
```

開発中はそのまま実行できる：

```bash
pip install -r requirements.txt
python -m mvdl            # GUI
python -m pytest tests    # テスト（ローカルの HTTP サーバーで確認）
```

## 構成

| パス | 役割 |
|---|---|
| `mvdl/gui.py` | 画面（tkinter） |
| `mvdl/finder.py` | 動画の検索（yt-dlp、HTML の解析） |
| `mvdl/probe.py` | 見つけた URL が動画かを確かめ、解像度・長さ・サイズを調べる |
| `mvdl/browser.py` | Edge を DevTools Protocol で操作し、通信から動画の URL を拾う（Playwright 不要） |
| `mvdl/downloader.py` | yt-dlp でのダウンロード、画質の選択、進捗 |
| `mvdl/tools.py` | exe の隣の ffmpeg / deno、Edge の場所 |

## 注意

- 保存してよい動画（自分の動画、利用規約や権利者が許可しているもの）にだけ使うこと。
- DRM（Widevine など）で保護された動画（Netflix、Amazon Prime Video など）は保存できない。保護を外す機能は付けない。
- Cloudflare を通った後でも、動画ファイル自体の配信にも確認がかかっているサイトでは、ダウンロードが 403 になることがある。
- サイトの仕様変更で yt-dlp が取れなくなったら、yt-dlp を更新してビルドし直す（Actions を再実行すれば最新の yt-dlp で作られる）。
