"""Download the WebView2 SDK assemblies the built-in browser uses into mvdl/lib (not kept in git).

  python tools/fetch_webview2.py
"""
import io
import os
import sys
import urllib.request
import zipfile

VERSION = '1.0.2903.40'
URL = f'https://www.nuget.org/api/v2/package/Microsoft.Web.WebView2/{VERSION}'
FILES = {  # path in the NuGet package -> file in mvdl/lib
    'lib/net462/Microsoft.Web.WebView2.Core.dll': 'Microsoft.Web.WebView2.Core.dll',
    'lib/net462/Microsoft.Web.WebView2.WinForms.dll': 'Microsoft.Web.WebView2.WinForms.dll',
    'runtimes/win-x64/native/WebView2Loader.dll': 'WebView2Loader.dll',
}


def main():
    dest = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'mvdl', 'lib')
    if all(os.path.isfile(os.path.join(dest, f)) for f in FILES.values()):
        print('mvdl/lib: up to date')
        return
    os.makedirs(dest, exist_ok=True)
    with urllib.request.urlopen(URL, timeout=120) as r:
        pkg = zipfile.ZipFile(io.BytesIO(r.read()))
    for src, name in FILES.items():
        with open(os.path.join(dest, name), 'wb') as f:
            f.write(pkg.read(src))
        print(f'mvdl/lib/{name}')


if __name__ == '__main__':
    sys.exit(main())
