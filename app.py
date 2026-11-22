#!/usr/bin/env python3
"""Readiverse 桌面版：用自己的視窗打開（pywebview，跟雅思練習室一樣），不用開瀏覽器。

python3 app.py            自己的視窗
python3 app.py --browser  用瀏覽器打開（舊的做法）
沒有 pywebview 時：這次先用瀏覽器打開，同時在背景安裝，下次就是自己的視窗。
"""
import os, sys, threading, time, subprocess, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE); sys.path.insert(0, HERE)
PORT = int(os.environ.get('READIVERSE_PORT') or 8780)
BASE = 'http://localhost:%d' % PORT
HOME = os.path.expanduser('~/.readiverse')
os.makedirs(HOME, exist_ok=True)


def ping():
    try:
        with urllib.request.urlopen(BASE + '/api/caps', timeout=1.5) as r: return r.status == 200
    except Exception: return False


def run_server(browser):
    import server
    sys.argv = ['server.py'] + ([] if browser else ['--no-browser'])
    server.main()


def mac_branding():
    """Dock 上顯示「Readiverse」和自己的圖示（不然會是 Python）"""
    if sys.platform != 'darwin': return
    try:
        from Foundation import NSBundle
        info = NSBundle.mainBundle().localizedInfoDictionary() or NSBundle.mainBundle().infoDictionary()
        info['CFBundleName'] = 'Readiverse'
    except Exception: pass
    try:
        from AppKit import NSApplication, NSImage
        icon = os.path.join(HERE, 'assets', 'icon.png')
        if os.path.exists(icon):
            NSApplication.sharedApplication().setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(icon))
    except Exception: pass


def browser_mode():
    if ping():
        import webbrowser; webbrowser.open(BASE + '/'); return
    run_server(True)


try:
    if '--browser' in sys.argv: raise ImportError('browser')
    import webview
except Exception:
    if '--browser' not in sys.argv and not getattr(sys, 'frozen', False):   # 沒有 pywebview：背景安裝，這次先用瀏覽器
        log = os.path.join(HOME, 'pywebview-install.log')
        if not os.path.exists(log) or time.time() - os.path.getmtime(log) > 1800:
            print('這次先用瀏覽器打開；視窗版（pywebview）正在背景安裝，下次打開就會是自己的視窗。')
            subprocess.Popen([sys.executable, '-m', 'pip', 'install', '--user', '--quiet', 'pywebview'],
                             stdout=open(log, 'w'), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
    browser_mode(); sys.exit(0)

mac_branding()
if not ping():                               # Readiverse 還沒開：在這個 App 裡啟動
    threading.Thread(target=run_server, args=(False,), daemon=True).start()

LOADING = ('<html><head><meta charset="utf-8"><style>body{margin:0;height:100vh;display:grid;place-items:center;'
           'font-family:-apple-system,"PingFang TC",sans-serif;background:#fff;color:#111}'
           '@media (prefers-color-scheme:dark){body{background:#111;color:#eee}}'
           '.b{font:600 40px Georgia,serif;letter-spacing:-.01em;display:flex;align-items:center;gap:14px}'
           '.d{width:18px;height:18px;border-radius:50%;background:#c2410c;animation:p 1.2s ease-in-out infinite}'
           '@keyframes p{50%{transform:scale(.6);opacity:.5}}p{color:#888;text-align:center;font-size:14px}'
           '</style></head><body><div><div class="b"><span class="d"></span>Readiverse</div><p>啟動中…</p></div></body></html>')
try: win = webview.create_window('Readiverse', html=LOADING, width=1440, height=920, min_size=(900, 600), text_select=True)
except TypeError: win = webview.create_window('Readiverse', html=LOADING, width=1440, height=920, min_size=(900, 600))


def boot():
    for _ in range(240):
        if ping(): break
        time.sleep(0.25)
    else:
        win.load_html('<p style="font-family:sans-serif;padding:30px">Readiverse 沒辦法啟動。請看 ~/.readiverse/app.log</p>'); return
    win.load_url(BASE + '/')


try:
    webview.settings['OPEN_EXTERNAL_LINKS_IN_BROWSER'] = True     # 劍橋字典之類的外部連結：用瀏覽器開
    webview.settings['ALLOW_DOWNLOADS'] = True                    # 匯出筆記
except Exception: pass
store = os.path.join(HOME, 'webview')                             # 記住設定（字體大小、外觀…）
os.makedirs(store, exist_ok=True)
try:
    try: webview.start(boot, private_mode=False, storage_path=store)
    except TypeError: webview.start(boot)
except Exception as e:
    print('視窗版打不開（%s），改用瀏覽器。' % e)
    import webbrowser; webbrowser.open(BASE + '/')
    while True: time.sleep(3600)
os._exit(0)                                                       # 關掉視窗：Readiverse 一起結束
