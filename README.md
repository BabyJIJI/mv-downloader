# mv-downloader（動画ダウンローダー）

Web ページに埋め込まれた動画を保存する Windows 用の GUI ツール。ブラウザを内蔵しているので、Cloudflare の確認やログインが必要なページは、自分で操作してから動画を探せる。

## 画面

左が操作コンソール、右がタブ。

| 左：操作コンソール | 右：タブ |
|---|---|
| ページの URL、「URL から探す」「ブラウザで開く」「ブラウザのページから探す」 | 見つかった動画（種類・解像度・長さ・サイズ・見つけ方） |
| 保存先、画質、「選んだ動画をダウンロードに追加」 | ブラウザ（戻る・進む・再読み込み・アドレスバー・「このページから探す」） |
| ダウンロード一覧の操作（同時に保存する数、中止、再試行、完了・失敗を消す、保存先を開く） | ダウンロード一覧（状態・進み具合） |
| | ログ |

## 使い方

### URL から探す（これまでどおり）

1. URL を入れて「URL から探す」
2. 「見つかった動画」から選び（複数可）、画質と保存先を選んで「選んだ動画をダウンロードに追加」
3. 「ダウンロード一覧」タブで進み具合を見る。保存中も、別のページを探して追加できる

探し方は、まず [yt-dlp](https://github.com/yt-dlp/yt-dlp)（YouTube・Vimeo など約 1,800 サイトに対応）、見つからなければページの HTML（`<video>`、`og:video`、スクリプト内の `.mp4` `.m3u8` `.mpd`、YouTube などの埋め込み）。

### ブラウザで探す（Cloudflare・ログイン・クリックしないと出ない動画）

1. 「ブラウザで開く」（または「ブラウザ」タブのアドレスバー）でページを開く
2. 確認画面やログインを済ませ、動画を再生する
3. 「ブラウザのページから探す」を押す

次の 3 つを合わせて一覧に出す。

- そのページでブラウザが読み込んだ動画（ファイルや HLS/DASH のマニフェスト）。別サイトのプレーヤー（iframe）の分も含む
- 押した時点のページの中身（JavaScript で書き換わった後の HTML）
- yt-dlp が対応しているサイトなら yt-dlp の結果（ブラウザの Cookie を使う）

ダウンロードには、ブラウザが実際に送った Cookie・User-Agent・Referer を使う。

ブラウザは閲覧中に何も操作しない（スクリプトの注入もクリックもしない）。読み込んだものの記録を取るだけで、ページを調べるのはボタンを押したときだけ。

「URL から探す」が Cloudflare に止められたときは、ブラウザで開くか聞かれる。

ブラウザの Cookie・ログイン・通った確認は `%LOCALAPPDATA%\mvdl\webview2` に残り、次回も使える（普段のブラウザとは別）。

### 一覧に出るもの

映像の入ったものだけ。見つけた URL は ffprobe（無ければ Content-Type）で中身を確かめ、音声だけ・画像・字幕・HTML などは除く。HLS は親のプレイリストだけを出し、その中の画質別・音声のプレイリストはまとめる。HLS のサイズは「ビットレート × 長さ」の見積もり（「約」が付く）。

## 使い方（配布版）

[Releases](../../releases) から取る。インストールは不要。

| ファイル | 中身 |
|---|---|
| `mvdl-win64.zip` | `mvdl.exe` ＋ `ffmpeg.exe` / `ffprobe.exe`（映像と音声の結合、HLS/DASH）＋ `deno.exe`（YouTube に必要）。展開して `mvdl.exe` を起動 |
| `mvdl.exe` | 本体だけ。ffmpeg が無いので、映像と音声が一体のファイルしか選べず、画質が下がることがある |

ブラウザには Windows の **Microsoft Edge WebView2 ランタイム**を使う（Windows 10/11 には通常入っている）。無いときは起動時に案内が出る。

設定（保存先・画質・同時に保存する数・最後に開いたページ）は `%APPDATA%\mvdl\settings.json`。

## ビルド

GitHub に push すると Actions（`.github/workflows/build.yml`）が、テスト（Linux）→ 実際のウィンドウでの動作確認（Windows、WebView2）→ exe のビルド → exe の起動確認、を行う。`v*` のタグを push すると Release に zip と exe を載せる。

```bash
git tag v0.5.0 && git push origin v0.5.0
```

手元の Windows で作るとき（Python 3.11 以上が必要。`winget install Python.Python.3.12`）：

```powershell
powershell -ExecutionPolicy Bypass -File build.ps1          # dist\mvdl-win64.zip
powershell -ExecutionPolicy Bypass -File build.ps1 -NoTools # exe だけ
```

開発中：

```powershell
pip install -r requirements.txt
python tools/fetch_webview2.py   # WebView2 SDK の DLL を mvdl/lib に取得
python -m mvdl                   # 起動
python -m pytest tests           # テスト（ローカルの HTTP サーバーで確認。Linux でも動く）
python tests/smoke_app.py        # 実際のウィンドウでの確認（Windows）
```

## 構成

| パス | 役割 |
|---|---|
| `mvdl/app.py` | 画面（WinForms ＋ WebView2、pythonnet 経由） |
| `mvdl/capture.py` | ブラウザが読み込んだ動画の記録（送った Cookie などのヘッダーも） |
| `mvdl/finder.py` | 動画の検索（yt-dlp、HTML の解析、ブラウザのページ） |
| `mvdl/probe.py` | 見つけた URL が動画かを確かめ、解像度・長さ・サイズを調べる |
| `mvdl/downloader.py` | yt-dlp でのダウンロード、画質の選択、進捗 |
| `mvdl/tools.py` | exe の隣の ffmpeg / deno |
| `tools/fetch_webview2.py` | WebView2 SDK（NuGet）の DLL の取得 |

## 注意

- 保存してよい動画（自分の動画、利用規約や権利者が許可しているもの）にだけ使うこと。
- DRM（Widevine など）で保護された動画（Netflix、Amazon Prime Video など）は保存できない。保護を外す機能は付けない。
- 動画ファイルの配信自体にも Cloudflare の確認がかかっているサイトでは、ブラウザで見つけた動画でもダウンロードが 403 になることがある。
- サイトの仕様変更で yt-dlp が取れなくなったら、yt-dlp を更新してビルドし直す（Actions を再実行すれば最新の yt-dlp で作られる）。
