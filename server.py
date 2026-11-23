#!/usr/bin/env python3
"""Readiverse 伺服器：只用 Python 內建套件。

書庫可以放在這台電腦，或放進 Google 雲端硬碟 / OneDrive 的同步資料夾（換電腦打開就是同一份）。
書庫位置記在 ~/.readiverse.json。

書庫/
  _index.json            所有書的清單（書名、作者、分類、標籤、進度、AI 整理進度…）、分類、使用者名字
  <書的 id>/book.pdf     原始 PDF
  <書的 id>/cover.jpg    封面（第一頁縮圖）
  <書的 id>/pages_raw.json  pdf.js 直接抽出來的段落（AI 整理前）
  <書的 id>/pages.json   每一頁的英文段落 [[段落, 段落…], …]（AI 整理過的會換掉）
  <書的 id>/state.json   螢光筆、筆記、中文翻譯
"""
import json, os, re, shutil, subprocess, sys, threading, time, uuid, webbrowser, queue, glob, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
import rv_dict

ROOT = os.path.dirname(os.path.abspath(__file__))


WIN = sys.platform.startswith('win')
if WIN:                                   # Windows：背景跑的程式（claude、Docling）不要跳出黑色命令視窗
    class _NoWindowPopen(subprocess.Popen):
        def __init__(self, *a, **k):
            k.setdefault('creationflags', 0x08000000)      # CREATE_NO_WINDOW
            super().__init__(*a, **k)
    subprocess.Popen = _NoWindowPopen


def _fix_path():
    """雙擊 App 打開時，macOS 只給很短的 PATH（找不到 claude、node、uv）：補上使用者登入時的 PATH"""
    if sys.platform.startswith('win'): return
    extra = []
    try:
        sh = os.environ.get('SHELL') or '/bin/zsh'
        r = subprocess.run([sh, '-lic', 'printf "__P__%s__P__" "$PATH"'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=8, stdin=subprocess.DEVNULL)
        m = re.search(r'__P__(.*?)__P__', r.stdout or '')
        if m: extra += m.group(1).split(':')
    except Exception: pass
    home = os.path.expanduser('~')
    extra += [home + '/.local/bin', home + '/.npm-global/bin', home + '/.cargo/bin', '/opt/homebrew/bin', '/usr/local/bin']
    cur = os.environ.get('PATH', '').split(':')
    os.environ['PATH'] = ':'.join(dict.fromkeys([p for p in cur + extra if p]))


_fix_path()
STATIC = os.path.join(ROOT, 'static')
CONFIG = os.path.expanduser('~/.readiverse.json')
LOCAL_LIB = os.path.expanduser('~/Readiverse書庫')       # 放在使用者資料夾：App 版不能讀「桌面」「文件」「下載」裡的檔案（macOS 的保護）
OLD_LIB = os.path.join(ROOT, '書庫')                       # 以前的位置（程式資料夾裡）：還在的話第一次打開時搬過去
CLOUD_DIRNAME = 'Readiverse書庫'
PORT = int(os.environ.get('READIVERSE_PORT') or 8780)
LOCK = threading.RLock()
ID_RE = re.compile(r'^[a-z0-9]{8,32}$')
DEFAULT_CATS = ['小說', '非小說', '學術', '語言學習']


def read_json(p, default=None):
    try:
        with open(p, encoding='utf-8') as f: return json.load(f)
    except Exception: return default


def write_json(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.%d.tmp' % threading.get_ident()
    with open(tmp, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, p)        # 先寫暫存檔再換掉：當機也不會寫壞


# ---------- 書庫位置（本機 / Google 雲端硬碟 / OneDrive）----------
LIB = os.environ.get('READIVERSE_LIB') or (read_json(CONFIG, {}) or {}).get('lib') or LOCAL_LIB
if not os.path.isdir(os.path.dirname(LIB)): LIB = LOCAL_LIB      # 雲端硬碟沒掛上：先用本機的


def index_path(): return os.path.join(LIB, '_index.json')


def storage_options():
    """這台電腦能放書庫的地方：本機、每個 Google 雲端硬碟帳號、OneDrive"""
    home = os.path.expanduser('~'); opts = [{'kind': 'local', 'label': '這台電腦', 'path': LOCAL_LIB}]
    seen = set()
    def add(kind, label, root):
        p = os.path.join(root, CLOUD_DIRNAME)
        if os.path.isdir(root) and p not in seen:
            seen.add(p); opts.append({'kind': kind, 'label': label, 'path': p})
    for d in sorted(glob.glob(os.path.join(home, 'Library', 'CloudStorage', 'GoogleDrive-*'))):       # Mac：Google Drive 電腦版
        acct = os.path.basename(d)[len('GoogleDrive-'):]
        for n in ('My Drive', '我的雲端硬碟', '我的云端硬盘'):
            add('gdrive', 'Google 雲端硬碟（%s）' % acct, os.path.join(d, n))
    for d in sorted(glob.glob(os.path.join(home, 'Library', 'CloudStorage', 'OneDrive*'))):            # Mac：OneDrive
        add('onedrive', 'OneDrive（%s）' % (os.path.basename(d)[9:] or '個人'), d)
    if sys.platform.startswith('win'):                                                                 # Windows
        for L in 'GHIJKLMNOPQRSTUVWXYZ':
            for n in ('My Drive', '我的雲端硬碟'): add('gdrive', 'Google 雲端硬碟（%s:）' % L, '%s:\\%s' % (L, n))
        for k in ('OneDrive', 'OneDriveConsumer', 'OneDriveCommercial'):
            if os.environ.get(k): add('onedrive', 'OneDrive', os.environ[k])
    for o in opts:
        o['current'] = os.path.abspath(o['path']) == os.path.abspath(LIB)
        o['hasLibrary'] = os.path.isfile(os.path.join(o['path'], '_index.json'))
    return opts


def move_library(target):
    """把書庫搬到 target。那邊已經有書庫（別台電腦放的）就合併：兩邊的書都留著。原本的資料夾不刪（當備份）"""
    global LIB
    with LOCK:
        if os.path.abspath(target) == os.path.abspath(LIB): return
        os.makedirs(target, exist_ok=True)
        src_ix = load_index(); tgt_ix = read_json(os.path.join(target, '_index.json'), None)
        if not isinstance(tgt_ix, dict): tgt_ix = {'books': [], 'categories': [], 'profile': {}}
        have = {b['id']: b for b in tgt_ix.get('books') or []}
        for b in src_ix['books']:
            sd, td = os.path.join(LIB, b['id']), os.path.join(target, b['id'])
            if b['id'] in have:                    # 兩邊都有：留比較新的那份
                if (b.get('opened') or b.get('added') or 0) <= (have[b['id']].get('opened') or have[b['id']].get('added') or 0): continue
                shutil.rmtree(td, ignore_errors=True)
            if os.path.isdir(sd): shutil.copytree(sd, td, dirs_exist_ok=True)
            have[b['id']] = b
        books = sorted(have.values(), key=lambda b: b.get('added') or 0, reverse=True)
        cats = list(dict.fromkeys((tgt_ix.get('categories') or []) + src_ix['categories']))
        prof = tgt_ix.get('profile') if (tgt_ix.get('profile') or {}).get('name') else src_ix.get('profile')
        LIB = target
        write_json(index_path(), {'books': books, 'categories': cats, 'profile': prof or {'name': ''}})
        cfg = read_json(CONFIG, {}) or {}; cfg['lib'] = target; write_json(CONFIG, cfg)


def load_index():
    j = read_json(index_path(), None)
    if not isinstance(j, dict): j = {}
    j.setdefault('books', []); j.setdefault('categories', list(DEFAULT_CATS))
    j.setdefault('profile', {'name': ''})
    j.pop('libPath', None)
    return j


def save_index(ix):
    ix = dict(ix); ix.pop('libPath', None); write_json(index_path(), ix)


def lib_view(ix):
    o = dict(ix); o['libPath'] = LIB
    o['storage'] = next((x for x in storage_options() if x['current']), {'kind': 'local', 'label': '這台電腦'})
    return o


def book_dir(bid):
    if not ID_RE.match(bid or ''): raise ValueError('bad id')
    return os.path.join(LIB, bid)


def find_book(ix, bid):
    for b in ix['books']:
        if b['id'] == bid: return b
    return None


def update_book(bid, **kw):
    with LOCK:
        ix = load_index(); b = find_book(ix, bid)
        if not b: return None
        b.update(kw); save_index(ix); return b


META_KEYS = ('title', 'author', 'category', 'tags', 'status', 'lastPage', 'lastPara', 'pages', 'favorite', 'hasText')


def clean_meta(d):
    out = {}
    for k in META_KEYS:
        if k not in d: continue
        v = d[k]
        if k in ('title', 'author', 'category', 'status'): v = str(v or '').strip()[:300]
        elif k == 'tags': v = [str(t).strip()[:40] for t in (v or []) if str(t).strip()][:20]
        elif k in ('lastPage', 'lastPara', 'pages'): v = max(0, int(v or 0))
        else: v = bool(v)
        out[k] = v
    return out


# ---------- AI：只用電腦上已登入的 Claude Code ----------
AI_BAD = {}       # 剛剛失敗的工具：10 分鐘內先跳過，省時間


AI_MODELS = [('claude-opus-5-5', 'Opus 5.5', '最強、最準，比較慢、比較花額度'), ('claude-sonnet-5-5', 'Sonnet 5.5', '準、比較快'), ('claude-haiku-5-5', 'Haiku 5.5', '最快、最省額度')]
AI_DEFAULT = {'best': 'claude-sonnet-5-5', 'fast': 'claude-haiku-5-5', 'top': 'claude-opus-5-5'}   # top：Sonnet 也解決不了的頁       # best：校對、整理頁面；fast：翻譯、書名、解釋


def ai_model(kind='fast'):
    m = ((read_json(CONFIG, {}) or {}).get('aiModels') or {}).get(kind)
    return m if m in [x[0] for x in AI_MODELS] else AI_DEFAULT[kind]


def claude_path():
    if WIN:
        home = os.path.expanduser('~'); appdata = os.environ.get('APPDATA') or os.path.join(home, 'AppData', 'Roaming')
        for c in (os.path.join(home, '.local', 'bin', 'claude.exe'), shutil.which('claude.exe'),
                  shutil.which('claude'), os.path.join(appdata, 'npm', 'claude.cmd')):
            if c and os.path.exists(c): return c
        return None
    c = shutil.which('claude') or os.path.expanduser('~/.local/bin/claude')
    return c if os.path.exists(c) else None


def claude_argv(c):
    """執行 claude 用的開頭。Windows 上 npm 裝的是 claude.cmd：改成 node cli.js，參數才不會被 cmd.exe 改掉"""
    if WIN and c and c.lower().endswith('.cmd'):
        js = os.path.join(os.path.dirname(c), 'node_modules', '@anthropic-ai', 'claude-code', 'cli.js')
        node = shutil.which('node') or os.path.join(os.path.dirname(c), 'node.exe')
        if os.path.exists(js) and node and os.path.exists(node): return [node, js]
    return [c]


def ai_cmds(prompt, model=None):
    """只用 Claude（電腦上登入的 Claude Code）"""
    claude = claude_path()
    if not claude: return []
    return [('Claude Code', [*claude_argv(claude), '-p', '--model', model or ai_model('fast'), prompt])]


def ai_auth():
    """Claude 登入狀態（claude auth status）"""
    c = claude_path()
    if not c: return {'installed': False, 'loggedIn': False}
    try:
        r = subprocess.run([*claude_argv(c), 'auth', 'status', '--json'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20, stdin=subprocess.DEVNULL)
        j = json.loads(r.stdout or '{}')
    except Exception: j = {}
    return {'installed': True, 'loggedIn': bool(j.get('loggedIn')), 'expires': j.get('loginExpiresAt') or '', 'method': j.get('authMethod') or ''}


LOGIN = {'state': '', 'url': '', 'error': '', 'out': '', 'fd': None, 'proc': None}


class _Term:
    """看不見的終端機（claude auth login 要在終端機裡跑）：Mac/Linux 用 pty，Windows 用 pywinpty"""
    def __init__(self, argv):
        if WIN:
            from winpty import PtyProcess
            self.w = PtyProcess.spawn(argv, dimensions=(40, 200)); self.fd = None
        else:
            import pty
            m, s_ = pty.openpty()
            self.p = subprocess.Popen(argv, stdin=s_, stdout=s_, stderr=s_, close_fds=True, start_new_session=True)
            os.close(s_); self.fd = m
    def read(self):
        if WIN:
            try: return self.w.read(4096).encode('utf-8', 'ignore')
            except EOFError: raise OSError('closed')
        import select
        r, _, _ = select.select([self.fd], [], [], 0.5)
        return os.read(self.fd, 4096) if r else b''
    def write(self, txt):
        if WIN: self.w.write(txt)
        else: os.write(self.fd, txt.encode())
    def alive(self):
        return self.w.isalive() if WIN else self.p.poll() is None
    def kill(self):
        try:
            if WIN: self.w.terminate(force=True)
            else: os.killpg(self.p.pid, 15)
        except Exception: pass
        try:
            if self.fd is not None: os.close(self.fd)
        except OSError: pass


def login_start():
    """在背景跑 claude auth login（看不見的終端機）：它會自己打開瀏覽器的授權頁；網址也抓下來，App 裡可以再按一次打開"""
    c = claude_path()
    if not c: return {'state': 'error', 'error': '這台電腦沒有 Claude Code'}
    login_stop()
    try: t = _Term(claude_argv(c) + ['auth', 'login'])
    except Exception as e: return {'state': 'error', 'error': '沒辦法開始登入：%s' % e, 'url': ''}
    LOGIN.update(state='waiting', url='', error='', out='', term=t)
    def pump():
        buf = b''
        while LOGIN.get('term') is t:
            try: buf += t.read()
            except OSError: break
            txt = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]|\x1b8;;[^\x1b]*\x1b', '', buf.decode('utf-8', 'ignore'))
            LOGIN['out'] = txt[-2000:]
            u = re.findall(r'https://[^\s\x1b\x07\]]+', buf.decode('utf-8', 'ignore'))
            if u and not LOGIN['url']: LOGIN['url'] = u[0]
            if 'Login successful' in txt or 'successfully' in txt.lower(): LOGIN['state'] = 'done'; break
            if not t.alive():
                if LOGIN['state'] == 'waiting': LOGIN.update(state='error', error=(txt.strip().splitlines() or ['登入失敗'])[-1][:200])
                break
            if WIN: time.sleep(0.2)
    threading.Thread(target=pump, daemon=True).start()
    return login_state()


def login_code(code):
    """授權頁給了一串代碼：貼回登入程式"""
    t = LOGIN.get('term')
    if not t or LOGIN['state'] != 'waiting': return login_state()
    try: t.write(str(code).strip() + '\r')
    except Exception as e: LOGIN.update(state='error', error=str(e))
    return login_state()


def login_stop():
    t = LOGIN.get('term')
    if t: t.kill()
    LOGIN.update(term=None)


def login_state():
    return {k: LOGIN[k] for k in ('state', 'url', 'error')}


def ai_test():
    """設定頁的「測試」：叫 Claude 回一個字，看登入、連線、模型能不能用"""
    c = claude_path()
    if not c: return {'ok': False, 'error': '這台電腦沒有 Claude Code', 'hint': '先安裝 Claude Code：https://claude.com/claude-code'}
    out = {}
    for kind in ('fast', 'best'):
        m = ai_model(kind); t = time.time()
        try:
            r = subprocess.run([*claude_argv(c), '-p', '--model', m, 'Reply with exactly: OK'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=90, stdin=subprocess.DEVNULL, cwd=os.path.expanduser('~'))
            msg = ((r.stdout or '') + (r.stderr or '')).strip()
            ok = r.returncode == 0 and 'OK' in (r.stdout or '')
        except subprocess.TimeoutExpired:
            ok, msg = False, '超過 90 秒沒有回應'
        out[kind] = {'ok': ok, 'model': m, 'secs': round(time.time() - t, 1), 'error': '' if ok else msg[-300:]}
    err = ' '.join(v['error'] for v in out.values())
    hint = '在終端機輸入 claude，再輸入 /login 重新登入' if re.search(r'auth|login|OAuth|expired|401', err, re.I) else ''
    return {'ok': all(v['ok'] for v in out.values()), 'path': c, 'tests': out, 'hint': hint}


def ai_json(prompt, want=list, timeout=300, kind='fast'):
    """問 AI，回傳 JSON（list 或 dict）。全部工具都失敗就丟 RuntimeError"""
    cmds = ai_cmds(prompt, ai_model(kind))
    if not cmds: raise RuntimeError('找不到 Claude Code：請先安裝並登入（終端機輸入 claude）')
    errs = []
    for name, cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout, stdin=subprocess.DEVNULL, cwd=os.path.expanduser('~'))
        except subprocess.TimeoutExpired:
            errs.append('%s 超過 %d 秒沒回應' % (name, timeout)); continue
        out = (r.stdout or '').strip()
        m = re.search(r'\[[\s\S]*\]' if want is list else r'\{[\s\S]*\}', out)
        try: val = json.loads(m.group(0)) if (r.returncode == 0 and m) else None
        except ValueError: val = None
        if isinstance(val, want):
            AI_BAD.pop(name, None); return val
        AI_BAD[name] = time.time()
        msg = (r.stderr or out or '沒有回應').strip().splitlines()
        errs.append('%s：%s' % (name, (msg[-1] if msg else '')[:200]))
    hint = '（Claude 登入過期的話，在終端機輸入 claude 重新登入）' if any('auth' in e.lower() for e in errs) else ''
    raise RuntimeError('AI 失敗：' + '；'.join(errs) + hint)




def all_notes():
    """首頁「我的筆記」：每一本書的筆記（螢光筆筆記、問 AI 的問答、段落筆記、頁筆記）集中在一起"""
    out = []
    for b in load_index()['books']:
        d = os.path.join(LIB, b['id']); st = read_json(os.path.join(d, 'state.json'), {}) or {}
        notes, paraN, pageN = st.get('notes') or {}, st.get('paraNotes') or {}, st.get('pageNotes') or {}
        if not (notes or paraN or pageN): continue
        pages = None
        def para_text(p, i):
            nonlocal pages
            if pages is None: pages = read_json(os.path.join(d, 'pages.json'), []) or []
            try: t = pages[p][i]; return t if isinstance(t, str) else ''
            except (IndexError, TypeError): return ''
        color = {}
        for k, L in (st.get('marks') or {}).items():
            for r in L or []:
                if len(r) > 3 and r[3]: color[r[3]] = r[2]
        base = {'bid': b['id'], 'title': b.get('title') or '', 'author': b.get('author') or ''}
        for nid, n in notes.items():
            if not str((n or {}).get('text') or '').strip(): continue
            out.append(dict(base, kind='note', id=nid, page=n.get('page') or 0, i=n.get('i') or 0, text=n['text'], quote=n.get('quote') or '',
                            t=n.get('t') or 0, c=color.get(nid, 0), qa=str(n['text']).lstrip().startswith('Q：')))
        for k, v in paraN.items():
            if not str(v or '').strip() or ':' not in k: continue
            p, i = (int(x) for x in k.split(':')[:2])
            out.append(dict(base, kind='para', id=k, page=p, i=i, text=v, quote=para_text(p, i)[:300], t=0, c=0, qa=str(v).lstrip().startswith('Q：')))
        for p, v in pageN.items():
            if not str(v or '').strip(): continue
            out.append(dict(base, kind='page', id='p' + str(p), page=int(p), i=0, text=v, quote='', t=0, c=0, qa=False))
    return out


def ai_text(prompt, timeout=240, kind='best'):
    """問 AI，回傳一般文字（問 AI 的回答）"""
    cmds = ai_cmds(prompt, ai_model(kind))
    if not cmds: raise RuntimeError('找不到 Claude Code：請先到設定登入 Claude')
    name, cmd = cmds[0]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout, stdin=subprocess.DEVNULL, cwd=os.path.expanduser('~'))
    except subprocess.TimeoutExpired:
        raise RuntimeError('AI 超過 %d 秒沒回應，請再試一次' % timeout)
    out = (r.stdout or '').strip()
    if r.returncode != 0 or not out:
        msg = ((r.stderr or out or '沒有回應').strip().splitlines() or [''])[-1][:200]
        raise RuntimeError(('Claude 登入過期，請到設定重新登入：' if re.search(r'auth|login|expired', msg, re.I) else 'AI 失敗：') + msg)
    return out


def ask_ai(j):
    """選取一段文字問 AI：帶這本書的書名、選到的字、那一段和前後各一段，還有前面的問答（可以一直追問）"""
    sel = str(j.get('sel') or '').strip()[:1500]; q = str(j.get('q') or '').strip()[:1000]
    if not q: raise ValueError('問題是空的')
    ctx = str(j.get('ctx') or '').strip()[:8000]; title = str(j.get('title') or '').strip()[:200]
    hist = [h for h in (j.get('history') or []) if isinstance(h, dict) and h.get('q') and h.get('a')][-8:]
    conv = ''.join('\n\nStudent asked: %s\nYou answered: %s' % (str(h['q'])[:1000], str(h['a'])[:3000]) for h in hist)
    prompt = ('You are a friendly English reading tutor helping a Taiwanese student who is reading an English book%s.\n'
              'Answer in Traditional Chinese (台灣用語). Quote English words or phrases when you explain them. '
              'Be clear and concise (usually under 200 字), use short paragraphs or a short list, no headings. '
              'You may use **bold** for key words. If the question is about grammar, vocabulary, meaning or nuance, explain with the context below.\n\n'
              'CONTEXT (the paragraph and its neighbours):\n%s\n\nSELECTED TEXT:\n%s%s\n\nStudent asks now: %s'
              % ((' titled "%s"' % title) if title else '', ctx or '(none)', sel or '(none)', conv, q))
    return {'a': ai_text(prompt, kind='best')}


def translate(paras):
    numbered = '\n\n'.join('[%d] %s' % (i + 1, p) for i, p in enumerate(paras))
    prompt = ('Translate each numbered English paragraph below into natural Traditional Chinese (台灣用語). '
              'Keep names and numbers. Reply with ONLY a JSON array of strings, one per paragraph, same order, '
              'exactly %d items, without the [n] numbers, no commentary.\n\n%s' % (len(paras), numbered))
    arr = [re.sub(r'^\s*\[\d+\]\s*', '', str(x)) for x in ai_json(prompt, list)]
    if len(arr) < len(paras): arr += [''] * (len(paras) - len(arr))
    return arr[:len(paras)]


# ---------- 文字清理（程式碼，不用 AI）：頁首頁尾、OCR 雜訊、跨頁斷字、圖表旁的碎字 ----------
def is_text(x): return isinstance(x, str)


def page_text(pg): return ''.join(x for x in pg if is_text(x))


def used_pages(state):
    """畫過螢光筆、寫過段落筆記、翻譯過的頁：段落不能再動，不然標記的位置會跑掉"""
    state = state or {}
    u = {int(k.split(':')[0]) for k, v in (state.get('marks') or {}).items() if v}
    u |= {int(k.split(':')[0]) for k, v in (state.get('paraNotes') or {}).items() if v}
    u |= {int(k) for k, v in (state.get('zh') or {}).items() if v}
    u |= {int(n.get('page')) for n in (state.get('notes') or {}).values() if isinstance(n, dict) and n.get('page') is not None}
    return u


def _hkey(s):
    """頁首的比對用：去掉前後的頁碼（「78 EMOTIONALLY CHARGED」），中間的數字保留（Questions 1-7 和 8-13 不一樣）"""
    s = re.sub(r'^\s*\d{1,4}\s+|\s+\d{1,4}\s*$', '', s.lower())
    return re.sub(r'[\W_]+', '', s)


def _headish(x):
    """像頁首頁尾的短句：短、不是句子（不以句點結尾）"""
    return len(x) <= 70 and not re.search(r'[.!?]["”’)]?$', x)


def _noise_tok(t):
    al = sum(c.isalnum() for c in t)
    return len(t) - al >= 2 and sum(c.isalpha() for c in t) <= 1


SUPS = str.maketrans('0123456789,', '⁰¹²³⁴⁵⁶⁷⁸⁹˒')
NOT_FN = re.compile(r'\b(?:no|nos|vol|vols|p|pp|fig|figs|ch|chap|chapter|part|table|sec|section|ed|op|art|para|v|vs|ver|step|unit|test|question|page|level|grade|room|act|scene)\.?\s?$', re.I)   # 這些字後面的數字不是註腳


def clean_pages(pages, frozen=()):
    """回傳清理過的 pages；frozen 裡的頁不動"""
    pages = [list(pg) for pg in pages]; n = len(pages); frozen = set(frozen)
    # 1) 段落內：軟連字號、段首的 OCR 雜訊（----=---~->）、多餘空白
    for p, pg in enumerate(pages):
        if p in frozen: continue
        out = []
        for x in pg:
            if is_text(x):
                x = re.sub(r'­\s*', '', x)
                toks = x.split()
                while toks and _noise_tok(toks[0]): toks.pop(0)
                x = ' '.join(toks)
                x = re.sub(r'^[•·*«»~&§¶|]+\s*(?=[a-z,.;:]|$)', '', x)        # 花紋被辨識成「•」「*」「«」（後面接小寫才是雜訊；「• System 1…」是真的條列）
                x = re.sub(r'^[·*«»~&§¶|]+\s*', '', x)
                x = re.sub(r'^[,.;:]+\s+', '', x)                            # 段落開頭多出來的標點
                x = re.sub(r'^(?:[-–]|[a-z])\s+(?=[A-Z"“‘])', '', x)         # 章節開頭的裝飾字被辨識成「-」或一個小寫字母
                x = re.sub(r'^[-–]\s+(?=[a-z])', '', x)                       # 「- twins, and they…」：換行的連字號跑到段落開頭
                x = re.sub(r'(\d)- (?=[A-Z])', r'\1. ', x)                       # 「summer of 1994- Looking」：句號被認成「-」
                x = re.sub(r'(?<=[.!?"”’)])\s?(\d{1,3}(?:,\s?\d{1,3})*)(?=\s+[A-Z"“‘]|\s*$)', lambda m: m.group(0) if NOT_FN.search(m.string[:m.start()]) else m.group(1).replace(' ', '').translate(SUPS), x)   # 註腳編號：書上的上標 ¹⁹ 被讀成一般數字「baseline. 19」→ 改回上標「baseline.¹⁹」
                if not x or re.fullmatch(r'(page\s*)?[\divxlc]{1,5}', x, re.I): continue      # 只有頁碼
                if re.search(r'©', x) and re.search(r'\bDOI\b|Press\b', x) and len(x) < 400: continue   # 出版社的版權聲明（每章開頭都有）
                if re.fullmatch(r'[A-HJ-Za-z]{1,2}|[^\w\s]{1,3}', x): continue           # 裝飾字、單獨一兩個字母（「N」）
            out.append(x)
        pages[p] = out
    # 2) 頁首頁尾：很多頁的開頭／結尾都出現的短句（書名、章名、「78 EMOTIONALLY CHARGED」）
    cnt = {}
    for p, pg in enumerate(pages):
        T = [x for x in pg if is_text(x)]
        for k in {_hkey(x) for x in T[:2] + T[-2:] if _headish(x)}:
            cnt.setdefault(k, []).append(p)
    # 頁首：在很多頁出現，而且是一頁接一頁（或隔一頁）重複；散落在書裡不同地方的（目錄、章名、注釋標題）不算
    heads = {k for k, ps in cnt.items() if len(ps) >= 3 and len(k) >= 6 and sum(1 for a, b in zip(ps, ps[1:]) if b - a <= 2) >= 2}
    if heads:
        seen = set()                                   # 第一次出現的留著（通常是真正的章名），之後重複的才是頁首
        for p, pg in enumerate(pages):
            T = [i for i, x in enumerate(pg) if is_text(x)]
            edge = set(T[:2] + T[-2:]); keep = []
            for i, x in enumerate(pg):
                k = _hkey(x) if is_text(x) and i in edge and _headish(x) else None
                if k in heads:
                    if k not in seen: seen.add(k); keep.append(x); continue
                    if p not in frozen: continue
                keep.append(x)
            pages[p] = keep
    # 3) 圖表旁邊的碎字（座標軸標籤之類）：只有幾個字、沒有句點，又緊貼著圖或表
    for p, pg in enumerate(pages):
        if p in frozen or all(is_text(x) for x in pg): continue
        keep = []
        for i, x in enumerate(pg):
            near = (i > 0 and not is_text(pg[i - 1])) or (i + 1 < len(pg) and not is_text(pg[i + 1]))
            if is_text(x) and near and len(x.split()) <= 4 and not re.search(r'[.!?:;]', x): continue
            # 圖表上面那段沒寫完、下面那段開頭黏了圖上的標籤（「Passive Active to avoid…」）→ 拿掉那幾個標籤字
            if is_text(x) and i > 0 and not is_text(pg[i - 1]):
                prev = next((y for y in reversed(pg[:i - 1]) if is_text(y)), '')
                m = re.match(r'((?:[A-Z][\w’\'-]*\s+){1,4})([a-z].*)$', x, re.S)
                if prev and not re.search(r'[.!?:;"”’)]$', prev.strip()) and m: x = m.group(2)
            keep.append(x)
        pages[p] = keep
    # 3.4) 小節開頭的花紋被認成小寫字（「me Now, five minutes later」）：上一段已經用句號結束，這段卻是小寫字＋大寫開頭 → 拿掉那個字
    last_t = ''
    for p, pg in enumerate(pages):
        for k, x in enumerate(pg):
            if not is_text(x): continue
            if p not in frozen and re.match(r'^[a-z]{1,4}\s+[A-Z][a-z]', x) and (not last_t or re.search(r'[.!?"”’)]$', last_t.strip()) or _headish(last_t)):
                x = re.sub(r'^[a-z]{1,4}\s+', '', x); pg[k] = x
            last_t = x
    # 3.45) 被圖表切斷的句子：圖上面那段沒寫完、圖下面那段小寫開頭 → 接回圖上面那段（圖放在段落後面）
    for p, pg in enumerate(pages):
        if p in frozen or all(is_text(x) for x in pg): continue
        k = 1
        while k + 1 < len(pg):
            if not is_text(pg[k]) and is_text(pg[k - 1]) and is_text(pg[k + 1]) and not re.search(r'[.!?:;"”’)\]—–]$', pg[k - 1].strip()) and re.match(r'[a-z]', pg[k + 1]):
                pg[k - 1] = pg[k - 1].rstrip() + ' ' + pg.pop(k + 1)
            k += 1
    # 3.5) 被切成兩段的句子：上一段沒有句尾標點、下一段小寫開頭 → 接起來（掃描書常見：「That was / the end of…」）
    for p, pg in enumerate(pages):
        if p in frozen: continue
        out = []
        for x in pg:
            prev = out[-1].strip() if out and is_text(out[-1]) else ''
            cont = is_text(x) and prev and not re.search(r'[.!?:;"”’)\]—–]$', prev) and (re.match(r'[a-z]', x) or (len(prev) > 60 and re.search(r'[a-z,]$', prev)))
            if cont:
                out[-1] = (out[-1][:-1] if re.search(r'[A-Za-z]-$', out[-1]) else out[-1] + ' ') + x
            else: out.append(x)
        pages[p] = out
    # 4) 跨頁的段落：這頁最後一段沒寫完、下一頁第一段是它的後半 → 整段接回這一頁（段落不要斷在兩頁）
    for p in range(n - 1):
        if p in frozen or (p + 1) in frozen: continue
        a = [i for i, x in enumerate(pages[p]) if is_text(x)]; b = [i for i, x in enumerate(pages[p + 1]) if is_text(x)]
        if not a or not b or a[-1] != len(pages[p]) - 1 or b[0] != 0: continue     # 中間隔著圖表：不接
        last, first = pages[p][a[-1]].strip(), pages[p + 1][b[0]].strip()
        if re.search(r'[.!?:;"”’)\]—–]$', last): continue
        if len(last.split()) < 8 and not re.match(r'[a-z(]', first): continue      # 上一頁最後是目錄、標題這種短行：不接
        if re.search(r'[A-Za-z]-$', last) and re.match(r'[a-z]', first):          # ex- / pected
            pages[p][a[-1]] = last[:-1] + first
        elif re.match(r'[a-z(]', first) or (len(last) > 60 and re.search(r'[a-z,]$', last)):
            pages[p][a[-1]] = last + ' ' + first
        else: continue
        pages[p + 1].pop(b[0])
    return pages


# ---------- 辨識品質：每一頁有多少「字典裡查不到的字」或怪符號（norm.alcy、W3S、himsel£）----------
QUALITY_THR = 0.04          # 超過 4% 的字有問題：算有亂碼
AUTO_FIX_MAX = 20           # 每本書最多自動請 AI 修幾頁（省額度）；剩下的頁碼旁邊顯示 ⚠，自己按 ✦ 修


def _known(w):
    D = rv_dict.local_dict().get('d', {})
    if not D: return True                                     # 沒有本機字典：不判斷
    w = w.lower().strip("'’")
    if w in D: return True
    for suf, r in (('ies', 'y'), ('es', ''), ('s', ''), ('ied', 'y'), ('ed', ''), ('ed', 'e'), ('ing', ''), ('ing', 'e'), ("'s", ''), ('’s', ''), ('ly', ''), ("n't", ''), ('n’t', '')):
        if w.endswith(suf) and (w[:-len(suf)] + r) in D: return True
    return False


def garble(pg):
    toks = re.findall(r'\S+', ' '.join(x for x in pg if is_text(x))); bad = n = 0
    for t in toks:
        core = t.strip('.,;:!?"“”‘’()[]—–-*')
        if not core: continue
        n += 1
        if re.search(r'[£<>~|\\{}^]|\d[A-Za-z]|[A-Za-z]\d|[a-z][.;:][a-z]', core): bad += 1; continue
        if re.fullmatch(r"[A-Za-z'’-]+", core) and core[0].islower() and '-' not in core and not _known(core): bad += 1
    return (bad / n if n else 0.0), bad


def bad_pages(pages):
    out = []
    for i, pg in enumerate(pages):
        sc, nb = garble(pg)
        if sc > QUALITY_THR and nb >= 3: out.append((sc, i))
    return [i for sc, i in sorted(out, reverse=True)]


QQ = queue.Queue()


def quality_worker():
    while True:
        bid = QQ.get()
        wait_session()
        try: quality_job(bid)
        except Exception as e: update_book(bid, fix={'status': 'error', 'error': str(e)[:300]})


def quality_job(bid):
    d = os.path.join(LIB, bid)
    with LOCK:
        pages = read_json(os.path.join(d, 'pages.json')) or []
        frozen = used_pages(read_json(os.path.join(d, 'state.json'), {}))
    bad = [i for i in bad_pages(pages) if i not in frozen]
    todo = bad[:AUTO_FIX_MAX]
    update_book(bid, fix={'status': 'running', 'done': 0, 'total': len(todo)})
    for k, i in enumerate(todo):
        try: new = ai_clean_page(pages[i])
        except RuntimeError as e:
            update_book(bid, fix={'status': 'error', 'error': str(e)[:300], 'done': k, 'total': len(todo)}); break
        with LOCK:
            cur = read_json(os.path.join(d, 'pages.json')) or []
            if i < len(cur) and i not in used_pages(read_json(os.path.join(d, 'state.json'), {})):
                cur[i] = new; write_json(os.path.join(d, 'pages.json'), cur); pages = cur
        update_book(bid, fix={'status': 'running', 'done': k + 1, 'total': len(todo), 'rev': int(time.time() * 1000)})
    else:
        update_book(bid, fix={'status': 'done', 'done': len(todo), 'total': len(todo)})
    with LOCK:
        pages = read_json(os.path.join(d, 'pages.json')) or []
        update_book(bid, ocrBad=bad_pages(pages))


# ---------- AI 校對：Claude 看原書每一頁的圖，逐字對照解析出來的文字，只回報差異；程式照著改（AI 不會整段重寫）----------
PROOFQ = queue.Queue()
READ_PROMPT = ("Read the image file %s. It is one page of a printed book. Our text extraction got NOTHING from this page "
    "(it may be a scanned image or a PDF we could not parse).\n"
    "Transcribe the body text of this page exactly as printed, word for word, in reading order. Rules:\n"
    "- One string per paragraph. Join lines inside a paragraph with spaces; join words hyphenated at a line end.\n"
    "- Headings (chapter titles, section titles, 'Part 1', 'Chapter IV') are their own strings.\n"
    "- Leave out the running header, the page number, and text that is part of a picture, chart or table.\n"
    "- Keep the original punctuation (em dashes, curly quotes). Footnote reference numbers as superscript digits (like ¹⁹).\n"
    "- Never add, summarize or correct anything. If the page has no body text (blank, picture only), return an empty list.\n"
    "Reply with ONLY JSON: {\"paras\": [\"...\", \"...\"]}")


def read_page_ai(bid, pno, imgdir, kind='fast'):
    """解析不出文字的頁：讓 AI 直接看頁面圖，把內文一段一段讀出來（掃描書、壞掉的 PDF 都可以）"""
    d = os.path.join(LIB, bid)
    img = os.path.join(imgdir, 'p%d.jpg' % (pno + 1))
    if not os.path.exists(img): return {'ok': False, 'error': '沒有頁面圖'}
    c = claude_path()
    if not c: raise RuntimeError('找不到 Claude Code')
    r = subprocess.run([*claude_argv(c), '-p', '--model', ai_model(kind), '--allowedTools', 'Read', '--add-dir', imgdir, '--', READ_PROMPT % img],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300, stdin=subprocess.DEVNULL, cwd=os.path.expanduser('~'))
    m = re.search(r'\{[\s\S]*\}', r.stdout or '')
    if r.returncode != 0 or not m:
        err = ((r.stderr or r.stdout or '').strip().splitlines() or ['沒有回應'])[-1][:200]
        if re.search(r'auth|login|expired', err, re.I): raise RuntimeError('Claude 沒有登入：' + err)
        if re.search(r'content filtering|blocked', err, re.I):              # AI 不肯照抄這一頁（有版權的內容會被擋）：記下來，不要每次都重試
            return {'ok': False, 'blocked': True, 'error': err, 't': int(time.time())}
        return {'ok': False, 'error': err}
    try: paras = [str(x).strip() for x in (json.loads(m.group(0)).get('paras') or []) if str(x).strip()]
    except (ValueError, AttributeError): return {'ok': False, 'error': 'AI 回傳的格式不對'}
    with LOCK:
        pages = read_json(os.path.join(d, 'pages.json')) or []
        frozen = used_pages(read_json(os.path.join(d, 'state.json'), {}))
        if pno < len(pages) and pno not in frozen and not any(is_text(x) for x in pages[pno]) and paras:
            pages[pno] = paras + [x for x in pages[pno] if not is_text(x)]      # 圖表留著，放在文字後面
            write_json(os.path.join(d, 'pages.json'), pages)
    return {'ok': True, 'read': len(paras), 't': int(time.time())}


PROOF_PROMPT = ("Read the image file %s. It is one page of a printed book.\n"
    "Below is the text we extracted. \"prev_tail\" is the end of the previous page's last paragraph (this page may start in the middle of it). "
    "\"paras\" are the text paragraphs on this page, keyed by paragraph index (figures and tables are handled separately; ignore their contents).\n"
    "Proofread word by word against the image, IN READING ORDER. Report ONLY real differences in the body text: missing words or sentences, misread words, "
    "words or sentences in the wrong place or order (for example a word that belongs at the end of prev_tail but was put at the start of paragraph 0 — "
    "fix it by inserting it in the right place and replacing the misplaced copy with an empty string), "
    "wrong punctuation (em dashes, quotes, apostrophes). Footnote reference numbers should be superscript digits (like ¹⁹) right after the word or punctuation.\n"
    "Ignore the running header and page number, line-end hyphenation, indentation and paragraph spacing. The LAST paragraph may continue past the bottom of this page (we join paragraphs that run onto the next page): text after the last line shown is NOT an error, never delete it.\n"
    "Reply with ONLY JSON:\n"
    "{\"fixes\":[{\"where\":\"prev_tail\" or a paragraph index,\"find\":\"exact short substring as given\",\"replace\":\"what the page says\"}],\n"
    " \"inserts\":[{\"where\":\"prev_tail\" or a paragraph index,\"after\":\"exact short substring as given\",\"insert\":\"missing text exactly as printed\"}],\n"
    " \"ok\":true or false}\n\n%s")


def render_pages(bid, nums):
    """把 PDF 的頁畫成圖（給 AI 看），存在 ~/.readiverse/proof/<書>/"""
    out = os.path.join(HOME_DIR, 'proof', bid); os.makedirs(out, exist_ok=True)
    todo = [n for n in nums if not os.path.exists(os.path.join(out, 'p%d.jpg' % n))]
    if todo and os.path.exists(VPY):
        code = ('import sys,pypdfium2 as P;d=P.PdfDocument(sys.argv[1]);o=sys.argv[2]\n'
                'for n in map(int,sys.argv[3:]): d[n-1].render(scale=2).to_pil().convert("RGB").save("%s/p%d.jpg"%(o,n),quality=85)')
        for k in range(0, len(todo), 50):
            subprocess.run([VPY, '-c', code, os.path.join(LIB, bid, 'book.pdf'), out] + [str(n) for n in todo[k:k + 50]], capture_output=True, timeout=600)
    return out


def apply_proof(pages, pno, j, frozen):
    """把 AI 的修正套進 pages（原地改）。AI 給的段落編號不一定準：
    先找它說的那段，找不到就在這頁（和上一頁最後一段）找唯一出現的地方。回傳 (applied, failed)"""
    applied, failed = [], []
    def spots():
        out = []
        if pno > 0 and (pno - 1) not in frozen:
            idx = max([i for i, x in enumerate(pages[pno - 1]) if is_text(x)] or [-1])
            if idx >= 0: out.append((pno - 1, idx))
        if pno not in frozen: out += [(pno, i) for i, x in enumerate(pages[pno]) if is_text(x)]
        return out
    def target(where, needle):
        S = spots(); first = None
        if where == 'prev_tail': first = S[0] if S and S[0][0] == pno - 1 else None
        else:
            try:
                k = int(where); T = [x for x in S if x[0] == pno]
                first = T[k] if 0 <= k < len(T) else None
            except (TypeError, ValueError): pass
        if first and pages[first[0]][first[1]].count(needle) == 1: return first
        hit = [x for x in S if needle in pages[x[0]][x[1]]]                 # 編號不對：整頁找
        return hit[0] if len(hit) == 1 and pages[hit[0][0]][hit[0][1]].count(needle) == 1 else None
    for f in j.get('fixes') or []:
        find, rep_ = str(f.get('find') or ''), str(f.get('replace') or '')
        if not find or len(rep_) > len(find) + 300: failed.append(f); continue
        t = target(f.get('where'), find)
        if not t:
            if rep_ and len(rep_) > 2 and any(rep_ in pages[x[0]][x[1]] for x in spots()): applied.append(dict(f, already=True))   # 已經改好了
            else: failed.append(f)
            continue
        W = lambda x: set(re.findall(r'[a-z]{2,}', x.lower()))
        if rep_ and rep_ in find: core = find.replace(rep_, ' ', 1).strip()
        elif not rep_: core = find.strip()
        else: core = '' if len(W(find) - W(rep_)) < 4 else find.strip()
        if len(core) >= 12 and len(W(core)) >= 2:                          # 會拿掉好幾個字：只准刪「別處也有一模一樣」的重複
            core = ' '.join(core.split()); nb = [x for x in spots() if x != t] + [(pno + 1, i) for i, x in enumerate(pages[pno + 1] if pno + 1 < len(pages) else []) if is_text(x)]
            if not any(core in pages[x[0]][x[1]] for x in nb): failed.append(f); continue
        pages[t[0]][t[1]] = pages[t[0]][t[1]].replace(find, rep_); applied.append(f)
    for f in j.get('inserts') or []:
        after, ins = str(f.get('after') or ''), str(f.get('insert') or '').strip()
        if not after or not ins or len(ins) > 1500: failed.append(f); continue
        t = target(f.get('where'), after)
        if not t or ins in pages[t[0]][t[1]]:
            if t or any(ins in pages[x[0]][x[1]] for x in spots()): applied.append(dict(f, already=True))
            else: failed.append(f)
            continue
        txt = pages[t[0]][t[1]]; k = txt.index(after) + len(after)
        left, right = txt[:k].rstrip(' '), txt[k:].lstrip(' ')
        pre = '' if ins[:1] in '.,;:!?)]”’' else ' '                          # 插入的字前後補空格（標點不補）
        post = '' if not right or right[:1] in '.,;:!?)]”’' else ' '
        pages[t[0]][t[1]] = left + pre + ins + post + right
        applied.append(f)
    if applied:
        for q in {pno - 1, pno}:                                           # 被清空的段落拿掉
            if 0 <= q < len(pages): pages[q] = [x for x in pages[q] if not is_text(x) or x.strip()]
    return applied, failed


def proof_page(bid, pno, imgdir, kind='best'):
    """校對一頁（pno 從 0 開始）。回傳這頁的紀錄"""
    d = os.path.join(LIB, bid)
    with LOCK:
        pages = read_json(os.path.join(d, 'pages.json')) or []
    if pno >= len(pages): return None
    paras = [x for x in pages[pno] if is_text(x)]
    if not paras: return {'ok': True, 'skip': 'no text'}
    prev = next((x for x in reversed(pages[pno - 1]) if is_text(x)), '') if pno > 0 else ''
    img = os.path.join(imgdir, 'p%d.jpg' % (pno + 1))
    if not os.path.exists(img): return {'ok': False, 'error': '沒有頁面圖'}
    payload = json.dumps({'prev_tail': prev[-200:], 'paras': {str(i): t for i, t in enumerate(paras)}}, ensure_ascii=False)
    c = claude_path()
    if not c: raise RuntimeError('找不到 Claude Code')
    r = subprocess.run([*claude_argv(c), '-p', '--model', ai_model(kind), '--allowedTools', 'Read', '--add-dir', imgdir, '--', PROOF_PROMPT % (img, payload)],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300, stdin=subprocess.DEVNULL, cwd=os.path.expanduser('~'))
    m = re.search(r'\{[\s\S]*\}', r.stdout or '')
    if r.returncode != 0 or not m:
        err = ((r.stderr or r.stdout or '').strip().splitlines() or ['沒有回應'])[-1][:200]
        if re.search(r'auth|login|expired', err, re.I): raise RuntimeError('Claude 沒有登入：' + err)
        if re.search(r'content filtering|blocked', err, re.I):              # AI 不肯照抄這一頁（有版權的內容會被擋）：記下來，不要每次都重試
            return {'ok': False, 'blocked': True, 'error': err, 't': int(time.time())}
        return {'ok': False, 'error': err}
    try: j = json.loads(m.group(0))
    except ValueError: return {'ok': False, 'error': 'AI 回傳的格式不對'}
    with LOCK:
        pages = read_json(os.path.join(d, 'pages.json')) or []
        frozen = used_pages(read_json(os.path.join(d, 'state.json'), {}))
        applied, failed = apply_proof(pages, pno, j, frozen)
        if applied: write_json(os.path.join(d, 'pages.json'), pages)
    return {'ok': bool(j.get('ok')) and not failed, 'applied': applied, 'failed': failed, 't': int(time.time())}


PROOF_ACTIVE = set()          # 正在校對的書（同一本不會同時跑兩份）


def proof_worker():
    while True:
        bid = PROOFQ.get()
        wait_session()
        with LOCK:
            if bid in PROOF_ACTIVE: continue
            PROOF_ACTIVE.add(bid)
        try: proof_job(bid)
        except Exception as e:
            import traceback; traceback.print_exc()
            update_book(bid, proof={'status': 'error', 'error': str(e)[:300]})
        finally:
            with LOCK: PROOF_ACTIVE.discard(bid)


PROOF_EVERY = 3      # Sonnet 抽查：每 3 頁 1 頁；有問題的頁再往前後各 2 頁檢查
PROOF_NEAR = 2
PROOF_FILES = {'fast': 'proof_fast.json', 'best': 'proof.json', 'top': 'proof_top.json'}
PROOF_TOP_NEAR = 5   # Sonnet 也修不好的頁：Opus 看這一頁和前後各 5 頁


def proof_job(bid):
    """AI 校對兩輪（跟原書的頁面圖逐字對照）：
    1. Haiku（快、便宜）：每一頁都校對一次
    2. Sonnet（最仔細）：每 3 頁抽 1 頁，加上可疑的頁（漏字、亂碼、Haiku 有修不了的地方）；
       抽到的頁 Sonnet 還找得到問題，就把前後各 2 頁也用 Sonnet 校對
    校對過的頁不會重做（關掉 App 再開會接著做）"""
    from concurrent.futures import ThreadPoolExecutor
    d = os.path.join(LIB, bid)
    with LOCK:
        pages = read_json(os.path.join(d, 'pages.json')) or []
    stop = [None]
    # 第 0 輪：解析不出文字的頁（沒有任何文字、也不是只有圖表的頁）→ AI 直接看頁面圖讀出來
    rd = read_json(os.path.join(d, 'read_ai.json'), {}) or {}
    empty = [i for i, pg in enumerate(pages) if not any(is_text(x) for x in pg) and not any(isinstance(x, dict) for x in pg) and not (rd.get(str(i + 1)) or {}).get('t')]
    if empty:
        update_book(bid, proof={'status': 'running', 'phase': 'read', 'done': 0, 'total': len(empty)})
        imgdir = render_pages(bid, [i + 1 for i in empty])
        def rone(i):
            if stop[0]: return
            try: r = read_page_ai(bid, i, imgdir)
            except RuntimeError as e: stop[0] = str(e); return
            with LOCK:
                fn = os.path.join(d, 'read_ai.json'); rr = read_json(fn, {}) or {}; rr[str(i + 1)] = r; write_json(fn, rr)
                b = find_book(load_index(), bid); pr = dict((b or {}).get('proof') or {}); pr['done'] = pr.get('done', 0) + 1
                pr['rev'] = int(time.time() * 1000); update_book(bid, proof=pr)
        with ThreadPoolExecutor(max_workers=3) as ex: list(ex.map(rone, empty))
        if stop[0]: update_book(bid, proof={'status': 'error', 'error': stop[0][:300]}); return
        with LOCK:
            pages = read_json(os.path.join(d, 'pages.json')) or []
            ix = load_index(); bb = find_book(ix, bid)
        if bb and not bb.get('hasText') and any(any(is_text(x) for x in pg) for pg in pages): update_book(bid, hasText=True)
    textp = [i for i, pg in enumerate(pages) if any(is_text(x) for x in pg)]
    if not textp: update_book(bid, proof={'status': 'done', 'done': 0, 'total': 0, 'fixed': 0}); return
    TP = set(textp)
    recs = lambda kind: read_json(os.path.join(d, PROOF_FILES[kind]), {}) or {}
    checked = lambda kind: {int(k) - 1 for k, v in recs(kind).items() if str(k).isdigit() and (v or {}).get('t')}
    def issues(v): return bool([a for a in (v or {}).get('applied') or [] if not a.get('already')] or (v or {}).get('failed'))
    def run_round(todo, kind, workers):
        imgdir = render_pages(bid, [i + 1 for i in todo])
        def one(i):
            if stop[0] or SESS['state'] != 'ok': return
            try: r = proof_page(bid, i, imgdir, kind)
            except RuntimeError as e: stop[0] = str(e); return
            if r is None: return
            with LOCK:
                fn = os.path.join(d, PROOF_FILES[kind]); rr = read_json(fn, {}) or {}; rr[str(i + 1)] = r; write_json(fn, rr)
                b = find_book(load_index(), bid); pr = dict((b or {}).get('proof') or {})
                pr['done'] = pr.get('done', 0) + 1; pr['rev'] = int(time.time() * 1000)
                update_book(bid, proof=pr)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, sorted(todo)))
    # 第 1 輪：Haiku 每一頁（Sonnet 已經看過的頁就不用）
    best_done = checked('best')
    want = [i for i in textp if i not in best_done]
    todo = sorted(set(want) - checked('fast'))
    if todo:
        update_book(bid, proof={'status': 'running', 'phase': 'fast', 'done': len(want) - len(todo), 'total': len(want)})
        run_round(todo, 'fast', 3)
    if stop[0]: update_book(bid, proof={'status': 'error', 'error': stop[0][:300]}); return
    # 第 2 輪：Sonnet 抽查（這本書設定成只做第 1 輪就跳過；之後可以再單獨補做）
    tops = [int(x) for x in ((find_book(load_index(), bid) or {}).get('proofTopPages') or []) if str(x).isdigit()]
    if tops:                                    # 指定交給 Opus 的頁（Sonnet 修不了的）：這一頁和前後各 5 頁
        near = int((find_book(load_index(), bid) or {}).get('proofTopNear', PROOF_TOP_NEAR))     # 0 = 只看指定的那幾頁
        tplan = {j for n in tops for j in range(n - 1 - near, n + near) if j in TP}
        todo = sorted(tplan - checked('top'))
        update_book(bid, proof={'status': 'running', 'phase': 'top', 'done': len(tplan) - len(todo), 'total': len(tplan)})
        if todo: run_round(todo, 'top', 1)
        if stop[0]: update_book(bid, proof={'status': 'error', 'error': stop[0][:300]}); return
        update_book(bid, proofTopPages=[], proofTopNear=PROOF_TOP_NEAR)
    if (find_book(load_index(), bid) or {}).get('proofFastOnly'):
        if (find_book(load_index(), bid) or {}).get('proofFigs'):     # 補做：只用 Sonnet 看有圖片、表格的頁和前後各一頁
            with LOCK: pages = read_json(os.path.join(d, 'pages.json')) or []
            figp = {i for i, pg in enumerate(pages) if any(isinstance(x, dict) for x in pg)}
            fplan = {j for i in figp for j in (i - 1, i, i + 1) if j in TP}
            todo = sorted(fplan - checked('best'))
            update_book(bid, proof={'status': 'running', 'phase': 'best', 'done': len(fplan) - len(todo), 'total': len(fplan)})
            if todo: run_round(todo, 'best', 2)
            if stop[0]: update_book(bid, proof={'status': 'error', 'error': stop[0][:300]}); return
            update_book(bid, proofFigs=False)
        update_book(bid, proof={'status': 'done', 'done': len(textp), 'total': len(textp), 'rev': int(time.time() * 1000), 'bestPending': True}); return
    lost = read_json(os.path.join(d, 'lost.json'), {}) or {}
    with LOCK: pages = read_json(os.path.join(d, 'pages.json')) or []
    flagged = {int(k) - 1 for k in lost if str(k).isdigit()} | set(bad_pages(pages))
    flagged |= {int(k) - 1 for k, v in recs('fast').items() if str(k).isdigit() and (v or {}).get('failed')}
    flagged |= {int(k) - 1 for k, v in (read_json(os.path.join(d, 'read_ai.json'), {}) or {}).items() if str(k).isdigit() and (v or {}).get('read')}
    # 有圖片、表格的頁：這一頁和前後各一頁一定要 Sonnet 看（圖表旁邊最容易漏字、段落接錯）
    figp = {i for i, pg in enumerate(pages) if any(isinstance(x, dict) for x in pg)}
    flagged |= {j for i in figp for j in (i - 1, i, i + 1)}
    seeds = set(textp[::PROOF_EVERY]) | (flagged & TP); plan = set(seeds)
    while True:
        done_set = checked('best')
        for k, v in recs('best').items():                               # Sonnet 在抽查的頁還找到問題：前後各 2 頁也要看
            if str(k).isdigit() and int(k) - 1 in seeds and issues(v):
                i = int(k) - 1; plan |= {j for j in range(i - PROOF_NEAR, i + PROOF_NEAR + 1) if j in TP}
        todo = sorted(plan - done_set)
        update_book(bid, proof={'status': 'running', 'phase': 'best', 'done': len(plan & done_set), 'total': len(plan)})
        if not todo or stop[0]: break
        run_round(todo, 'best', 2)
    total = len(plan)
    if stop[0]: update_book(bid, proof={'status': 'error', 'error': stop[0][:300]}); return
    # 第 3 輪：Sonnet 也解決不了的頁（修正套不上去、或者看完還是像亂碼）→ Opus 看這一頁和前後各 5 頁
    with LOCK:
        pages = read_json(os.path.join(d, 'pages.json')) or []
        frozen = used_pages(read_json(os.path.join(d, 'state.json'), {}))
    hard = set()
    for k, v in recs('best').items():
        F = (v or {}).get('failed') or []
        if not F or not str(k).isdigit() or int(k) - 1 >= len(pages): continue
        test = json.loads(json.dumps(pages))                            # 先試套一次（不存）：現在套得上去就不算
        _, fl = apply_proof(test, int(k) - 1, {'fixes': [f for f in F if 'find' in f], 'inserts': [f for f in F if 'after' in f]}, frozen)
        if fl: hard.add(int(k) - 1)
    hard |= set(bad_pages(pages)) & checked('best')
    top_plan = {j for i in hard for j in range(i - PROOF_TOP_NEAR, i + PROOF_TOP_NEAR + 1) if j in TP}
    todo = sorted(top_plan - checked('top'))
    if todo:
        update_book(bid, proof={'status': 'running', 'phase': 'top', 'done': len(top_plan) - len(todo), 'total': len(top_plan)})
        run_round(todo, 'top', 1)
        if stop[0]: update_book(bid, proof={'status': 'error', 'error': stop[0][:300]}); return
    fixed = 0
    with LOCK:                                                              # 之前沒套上的修正再套一次（不用再問 AI）
        pages = read_json(os.path.join(d, 'pages.json')) or []
        frozen = used_pages(read_json(os.path.join(d, 'state.json'), {})); chp = False
        for kind in ('fast', 'best', 'top'):
            fn = os.path.join(d, PROOF_FILES[kind]); rr = read_json(fn, {}) or {}; ch = False
            for k, v in rr.items():
                F = (v or {}).get('failed') or []
                if not F or not k.isdigit() or int(k) - 1 >= len(pages): continue
                ap, fl = apply_proof(pages, int(k) - 1, {'fixes': [f for f in F if 'find' in f], 'inserts': [f for f in F if 'after' in f]}, frozen)
                if ap: v['applied'] = (v.get('applied') or []) + ap; v['failed'] = fl; ch = chp = True
            if ch: write_json(fn, rr)
            fixed += sum(len([a for a in v.get('applied') or [] if not a.get('already')]) for v in rr.values() if isinstance(v, dict))
        if chp: write_json(os.path.join(d, 'pages.json'), pages)
    with LOCK:
        pages = read_json(os.path.join(d, 'pages.json')) or []
    update_book(bid, proof={'status': 'done', 'done': total, 'total': total, 'fixed': fixed, 'rev': int(time.time() * 1000)}, ocrBad=bad_pages(pages))


# ---------- Docling：版面解析（圖片、表格、段落）。裝在 Readiverse 自己的 Python 3.12 環境 ----------
HOME_DIR = os.path.expanduser('~/.readiverse')
VENV = os.path.join(HOME_DIR, 'docling-venv')
VPY = os.path.join(VENV, 'Scripts', 'python.exe') if sys.platform.startswith('win') else os.path.join(VENV, 'bin', 'python')
DOCLING = {'status': 'checking', 'error': ''}      # checking / installing / ready / unavailable / error
DOCLING_READY = threading.Event()


def _find_uv(install=False):
    ext = '.exe' if WIN else ''
    for c in (shutil.which('uv'), os.path.expanduser('~/.local/bin/uv' + ext), os.path.expanduser('~/.cargo/bin/uv' + ext), '/opt/homebrew/bin/uv'):
        if c and os.path.exists(c): return c
    if not install: return None
    try:                                  # 沒有 uv：用官方的安裝程式裝（裝在使用者自己的資料夾，不用管理員權限）
        if WIN: subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', 'irm https://astral.sh/uv/install.ps1 | iex'], capture_output=True, timeout=600)
        else: subprocess.run(['sh', '-c', 'curl -LsSf https://astral.sh/uv/install.sh | sh'], capture_output=True, timeout=600)
    except Exception: pass
    return _find_uv(False)


def _find_py310():
    cands = [shutil.which(x) for x in ('python3.13', 'python3.12', 'python3.11', 'python3.10', 'python3', 'python')]
    cands += [os.path.expanduser('~/.local/bin/python3.12'), '/opt/homebrew/bin/python3', '/usr/local/bin/python3']
    for c in cands:
        if not c or not os.path.exists(c): continue
        try:
            v = subprocess.run([c, '-c', 'import sys;print(sys.version_info[:2]>=(3,10))'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=20).stdout.strip()
            if v == 'True': return c
        except Exception: pass
    return None


def docling_setup(force=False):
    """檢查 Docling 能不能用，不能用就在背景安裝（約 2 GB，第一次要幾分鐘）"""
    PKGS = ['docling'] + (['ocrmac'] if sys.platform == 'darwin' else [])     # ocrmac：用 Mac 內建的 Apple Vision 辨識掃描書
    def ok(mod='docling'):
        try: return os.path.exists(VPY) and subprocess.run([VPY, '-c', 'import ' + mod], capture_output=True, timeout=120).returncode == 0
        except Exception: return False
    if ok():
        if sys.platform == 'darwin' and not ok('ocrmac'):           # 以前裝的環境沒有 ocrmac：補裝（很快）
            uv = _find_uv()
            try: subprocess.run([uv, 'pip', 'install', '-q', '-p', VPY, 'ocrmac'] if uv else [VPY, '-m', 'pip', 'install', '-q', 'ocrmac'], capture_output=True, timeout=600)
            except Exception: pass
        DOCLING.update(status='ready', error=''); DOCLING_READY.set(); return
    if os.environ.get('READIVERSE_NO_DOCLING'):
        DOCLING.update(status='unavailable', error='已關閉'); return
    os.makedirs(HOME_DIR, exist_ok=True)
    log = open(os.path.join(HOME_DIR, 'docling-install.log'), 'a', encoding='utf-8')
    DOCLING.update(status='installing', error='')
    try:
        uv = _find_uv(install=True)
        if uv:
            cmds = [[uv, 'venv', '-q', '--allow-existing', '-p', '3.12', VENV], [uv, 'pip', 'install', '-q', '-p', VPY] + PKGS]
        else:
            py = _find_py310()
            if not py:
                DOCLING.update(status='unavailable', error='需要 Python 3.10 以上（或 uv）才能裝 Docling'); return
            cmds = [[py, '-m', 'venv', VENV], [VPY, '-m', 'pip', 'install', '-q'] + PKGS]
        for c in cmds:
            log.write('$ %s\n' % ' '.join(c)); log.flush()
            r = subprocess.run(c, stdout=log, stderr=log, timeout=3600)
            if r.returncode != 0: raise RuntimeError('安裝失敗（看 ~/.readiverse/docling-install.log）')
        if not ok(): raise RuntimeError('裝好了但載入失敗（看 ~/.readiverse/docling-install.log）')
        DOCLING.update(status='ready', error=''); DOCLING_READY.set()
    except Exception as e:
        DOCLING.update(status='error', error=str(e)[:300])
    finally:
        log.close()



def _wds(t): return re.findall(r"[a-z0-9]+", str(t).lower().replace('’', "'"))


def _bigrams(ws): return set(zip(ws, ws[1:]))


def raw_repeats(raw_pages, n=3):
    """PDF 文字裡在 3 頁以上重複出現的段落（頁首、版權聲明）：不能拿來補"""
    from collections import Counter
    key = lambda t: re.sub(r'[^a-z]', '', str(t).lower())
    c = Counter(k for pg in raw_pages for k in {key(x) for x in (pg if isinstance(pg, list) else []) if is_text(x)} if k)
    return {k for k, v in c.items() if v >= n}


def restore_missing(items, raw_paras, near_items, repeats=frozenset()):
    """數位書：Docling 有時候會整段漏掉（版面模型沒框到）。拿 PDF 本身的文字（匯入時 pdf.js 抽的，順序和內容都完整）對照：
    這一頁（含前後頁，跨頁接起來的段落）找不到的段落，放回它在原書的位置。回傳 (新的 items, 補回幾段)"""
    have = set()
    for x in list(items) + list(near_items):
        if is_text(x): have |= _bigrams(_wds(x))
        elif isinstance(x, dict): have |= _bigrams(_wds(' '.join([x.get('cap') or ''] + [' '.join(map(str, r)) for r in (x.get('rows') or [])])))
    has_fig = any(isinstance(x, dict) for x in items)
    out = list(items); added = 0
    def cover(t):
        bg = _bigrams(_wds(t)); return (len(bg & have) / len(bg)) if bg else 1.0
    def find_pos(t):                        # 這一段在 Docling 那一頁的哪一個 item 裡（找最像的）
        bg = _bigrams(_wds(t)); best, bi = 0, -1
        for k, x in enumerate(out):
            if is_text(x):
                o = len(bg & _bigrams(_wds(x)))
                if o > best: best, bi = o, k
        return bi if best >= 2 else -1
    for ri, r in enumerate(raw_paras):
        if not is_text(r): continue
        ws = _wds(r); rq = _sq(r)
        short_talk = len(ws) < 8 and re.match(r'^["“‘].*["”’]$', rq) and len(ws) >= 1      # 很短的對話（「"Indeed!"」）
        if short_talk:
            alltxt = ' '.join(_sq(x) for x in list(out) + list(near_items) if is_text(x))
            if rq in alltxt: continue
        elif len(ws) < 8 or sum(c.isalpha() for c in r) < 0.6 * len(r.replace(' ', '')): continue      # 太短、不像句子（頁碼、圖裡的字）
        if re.sub(r'[^a-z]', '', r.lower()) in repeats: continue                                     # 每頁都有的頁首、版權聲明
        L = [c for c in r if c.isalpha()]
        if L and sum(c.isupper() for c in L) > 0.6 * len(L): continue                                 # 全大寫：頁首、章名頁的裝飾字
        if has_fig and (len(ws) < 15 or not re.search(r'[.!?]["”’)]?$', r.strip())): continue      # 有圖的頁：只補像正文的長段落
        if not short_talk and cover(r) >= 0.4: continue
        pos = len(out)                      # 放在：後面第一個 Docling 有的段落前面；找不到就放在前一段後面
        for r2 in raw_paras[ri + 1:]:
            if is_text(r2) and cover(r2) >= 0.6:
                k = find_pos(r2)
                if k >= 0: pos = k; break
        else:
            for r0 in reversed(raw_paras[:ri]):
                if is_text(r0) and cover(r0) >= 0.6:
                    k = find_pos(r0)
                    if k >= 0: pos = k + 1; break
        t = re.sub(r'\s+', ' ', r).strip()
        out.insert(pos, t); have |= _bigrams(_wds(t)); added += 1
    return out, added



def _sq(t): return re.sub(r'\s+', ' ', str(t)).strip()


def split_by_raw(pages, raw, i, frozen=()):
    """數位書：Docling 常把「首行縮排、中間沒空行」的好幾段黏成一段。用 PDF 本身的分段（pdf.js）在原書分段的地方切開；
    切出來的段落如果原本在下一頁（跨頁接過來的），搬回下一頁開頭。回傳切了幾刀"""
    if i in frozen or i >= len(pages) or i >= len(raw): return 0
    starts = []                                         # (開頭的字, 原本在第幾頁)
    for q in (i, i + 1):
        if q < len(raw) and q not in frozen:
            for r in raw[q] or []:
                if is_text(r) and (len(_wds(r)) >= 4 or (re.match(r'^["“‘]', _sq(r)) and len(_sq(r)) >= 4)):     # 對話：短到「"No."」也是一段
                    starts.append((_sq(r)[:32], q))
    if not starts: return 0
    cuts = 0; out = []
    dn = lambda z: re.sub(r'[—–]', '-', z)            # 比對時破折號、連字號當成一樣（長度不變，位置對得上）
    for x in pages[i]:
        if not is_text(x): out.append((x, i)); continue
        t = _sq(x); tn = dn(t); pos = []
        for key, q in starts:
            key = dn(key); k = tn.find(key, 1)
            while k > 0:
                if re.search(r'[.!?:;"”’)\]]\s$', t[max(0, k - 3):k]) or t[k - 1:k] == ' ' and re.search(r'[.!?"”’)]$', t[:k].rstrip()):
                    pos.append((k, q)); break
                k = tn.find(key, k + 1)
        if not pos: out.append((x, i)); continue
        pos = sorted(set(pos)); prev = 0; src = i
        for k, q in pos:
            out.append((t[prev:k].strip(), src)); prev, src = k, q; cuts += 1
        out.append((t[prev:].strip(), src))
    stay = [x for x, q in out if q == i and (not is_text(x) or x)]
    move = [x for x, q in out if q == i + 1 and x]
    if i + 1 < len(pages):                              # 下一頁本來就有的（重複了）：不搬，直接拿掉
        nxt = {_sq(y)[:40] for y in pages[i + 1] if is_text(y)}
        move = [x for x in move if not (is_text(x) and _sq(x)[:40] in nxt)]
    if move and i + 1 < len(pages) and (i + 1) not in frozen and all(is_text(m) for m in move):
        pages[i + 1] = move + pages[i + 1]
    else: stay += move
    pages[i] = stay
    return cuts




def is_heading(t):
    """像章名、小標題：很短、不是句子（沒有句尾標點）、大寫開頭"""
    t = _sq(t)
    if not t or len(t) > 70 or len(_wds(t)) > 10: return False
    if re.search(r'[.!?,;:]["”’\')]*$', t) or not re.match(r'[A-Z0-9(]', t): return False      # 句子、對話（引號開頭）都不是標題
    return sum(c.isalpha() for c in t) >= 0.6 * len(t.replace(' ', ''))


def move_trailing_headings(pages, frozen=()):
    """標題剛好在一頁的最後（內文在下一頁）：標題搬到下一頁開頭，跟它的內文在一起。回傳搬了幾個"""
    n = 0
    for i in range(len(pages) - 1):
        if i in frozen or (i + 1) in frozen: continue
        pg = pages[i]
        k = len(pg)
        while k > 0 and is_text(pg[k - 1]) and is_heading(pg[k - 1]): k -= 1
        tail = pg[k:]
        if not tail or k == 0: continue                     # 整頁都是標題（章節頁）：不動
        nxt = pages[i + 1]
        first = next((x for x in nxt if is_text(x)), '')
        if not first or is_heading(first) or len(_wds(first)) < 8: continue          # 下一頁開頭不是內文（目錄、版權頁、章節頁）：不搬
        if not any(is_text(x) and not is_heading(x) and len(_wds(x)) >= 8 for x in pg[:k]): continue   # 這一頁沒有內文（目錄頁）：不搬
        pages[i] = pg[:k]; pages[i + 1] = tail + nxt; n += len(tail)
    return n


def dedupe_page(items):
    """同一頁裡一模一樣的段落只留最後一份（前面那份通常是從上一頁搬過來、位置不對的）"""
    seen, out = set(), []
    for x in reversed(items):
        if is_text(x):
            k = ' '.join(_wds(x))                       # 只比字（破折號、引號寫法不同也算同一段）
            if (len(k) > 30 or re.match(r'^["“‘]', _sq(x))) and k in seen: continue
            seen.add(k)
        out.append(x)
    return out[::-1]


def restore_headings(items, raw_paras, near_items, repeats=frozenset()):
    """原書有、Docling 漏掉的小標題（很短、不是句子）：放回它前後段落中間"""
    repeats_h = repeats                                  # 呼叫的人給「出現 6 頁以上」的頁首（標題本身會在目錄、章節頁重複幾次）
    qn = lambda t: re.sub(r'[^a-z0-9]', '', str(t).lower())
    have = {qn(x) for x in list(items) + list(near_items) if is_text(x)}
    out = list(items); n = 0
    for ri, r in enumerate(raw_paras or []):
        if not is_text(r): continue
        t = _sq(r); ws = _wds(t)
        if not (1 <= len(ws) <= 10) or len(t) > 70 or re.search(r'[.!?,;:]["”’)]?$', t) or not re.match(r'[A-Z0-9"“‘]', t): continue
        if sum(c.isalpha() for c in t) < 0.6 * len(t.replace(' ', '')): continue
        if re.sub(r'[^a-z]', '', t.lower()) in repeats_h or qn(t) in have or re.fullmatch(r'[\d\s.]+', t): continue
        nxt = next((_sq(r2)[:32] for r2 in raw_paras[ri + 1:] if is_text(r2) and len(_wds(r2)) >= 6), '')
        k = next((j for j, x in enumerate(out) if is_text(x) and nxt and _sq(x).startswith(nxt)), -1)
        if k < 0: continue                              # 找不到它後面那一段：不確定放哪，不補
        out.insert(k, t); have.add(qn(t)); n += 1
    return out, n


def fix_dashes(items, raw_text):
    """Docling 會把「—」變成「-」：pdf.js 抽的原始文字裡同一個地方是「—」就換回來"""
    if '—' not in raw_text and '–' not in raw_text: return items
    flat = re.sub(r'\s+', ' ', raw_text)
    def fix(t):
        def rp(m):
            a, b = t[max(0, m.start() - 12):m.start()], t[m.end():m.end() + 12]
            for d in ('—', '–'):
                if a + d + b in flat or (a[-6:] + d + b[:6]) in flat: return d
            return m.group(0)
        return re.sub(r'(?<=\w)-(?=\w)', rp, t)
    return [fix(x) if is_text(x) else x for x in items]


PARSEQ = queue.Queue()
PARSE_PROC = {}


PARSE_PENDING = set()       # 已經在排隊的書（同一本不要排兩次：跑兩次會把「這次不要校對」用掉）


def parse_put(bid):
    with LOCK:
        if bid in PARSE_PENDING: return False
        PARSE_PENDING.add(bid)
    PARSEQ.put(bid); return True


def parse_worker():
    while True:
        bid = PARSEQ.get()
        with LOCK: PARSE_PENDING.discard(bid)
        wait_session()
        DOCLING_READY.wait()
        try: parse_job(bid)
        except Exception as e:
            import traceback; traceback.print_exc()
            update_book(bid, parse={'status': 'error', 'error': str(e)[:300]})


def parse_job(bid):
    with LOCK:
        b = find_book(load_index(), bid)
    if not b: return
    d = os.path.join(LIB, bid); pdf = os.path.join(d, 'book.pdf'); figdir = os.path.join(d, 'figs')
    st = dict(b.get('parse') or {}); start = int(st.get('next') or 0) + 1
    update_book(bid, parse={'status': 'running', 'next': start - 1, 'total': b.get('pages') or 0})
    p = subprocess.Popen([VPY, os.path.join(ROOT, 'docling_worker.py'), pdf, figdir, '20', str(start)],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8')
    PARSE_PROC[bid] = p; total = b.get('pages') or 0; got = False; lost = {}; scanned = bool(b.get('scanned')); raw_rep = None; raw_rep6 = None
    try:
        for line in p.stdout:
            try: j = json.loads(line)
            except ValueError: continue
            if 'total' in j:
                total = j['total']; scanned = bool(j.get('scanned')); update_book(bid, scanned=scanned, ocr=j.get('ocr') or ''); continue
            if 'lost' in j:                                   # 漏字檢查：記下來（AI 校對會特別注意）
                lost[str(j['lost'])] = j.get('words') or []; continue
            if 'to' not in j: continue                        # 其他訊息（警告）
            with LOCK:
                if not find_book(load_index(), bid): p.kill(); return      # 書被刪掉了
                if 'pages' in j:
                    got = True
                    pf = os.path.join(d, 'pages.json'); pages = read_json(pf) or []
                    while len(pages) < total: pages.append([])
                    frozen = used_pages(read_json(os.path.join(d, 'state.json'), {}))
                    raw = read_json(os.path.join(d, 'pages_raw.json')) or []
                    for k, items in j['pages'].items():
                        i = int(k) - 1
                        if i in frozen or not (0 <= i < len(pages)): continue
                        new = [re.sub(r'\s+', ' ', x).strip() if is_text(x) else x for x in items]
                        # 有些電子書的 PDF 夾帶網頁碼殘渣（height="0%">、<p 、fileposid=…）：拿掉
                        new = [re.sub(r'\s{2,}', ' ', re.sub(r'\s*(?:<\s*[A-Za-z][^<>]{0,40}>|[\w%]*="[^"<>]{0,15}">)\s*', ' ', x)).strip() if is_text(x) else x for x in new]
                        new = [re.sub(r'%">\S*', '', x) if is_text(x) else x for x in new]          # 「It%">5¢. is」→「It is」
                        new = [x for x in new if not is_text(x) or x]
                        if i < len(raw): new = fix_dashes(new, ' '.join(x for x in raw[i] if is_text(x)))
                        if i < len(raw) and not scanned and not j.get('lineMode'):   # Docling 漏掉的段落：用 PDF 本身的文字補回來（照行的位置排的不需要）
                            nb = [x for q in (i - 1, i + 1) if 0 <= q < len(pages) for x in (j['pages'].get(str(q + 1)) or pages[q])]
                            if raw_rep is None: raw_rep = raw_repeats(raw)
                            new, nadd = restore_missing(new, raw[i], nb, raw_rep)
                            if nadd: lost[str(i + 1)] = (lost.get(str(i + 1)) or []) + ['（Docling 漏掉 %d 段，已用 PDF 文字補回）' % nadd]
                        if page_text(new).strip() or any(not is_text(x) for x in new) or not page_text(pages[i]).strip():
                            pages[i] = new
                    pages = clean_pages(pages, frozen)
                    if not scanned and raw and not j.get('lineMode'):   # 數位書（舊做法）：照原書的分段切開黏在一起的段落、補回漏掉的小標題
                        if raw_rep6 is None: raw_rep6 = raw_repeats(raw, 6)
                        for k in sorted(int(x) - 1 for x in j['pages']):
                            if k in frozen or not (0 <= k < len(pages)): continue
                            split_by_raw(pages, raw, k, frozen)
                            nb = [x for q in (k - 1, k + 1) if 0 <= q < len(pages) for x in pages[q]]
                            if k < len(raw): pages[k], _ = restore_headings(pages[k], raw[k], nb, raw_rep6)
                            pages[k] = dedupe_page(pages[k])
                    move_trailing_headings(pages, frozen)              # 頁尾的標題：跟下一頁的內文放在一起
                    write_json(pf, pages)
                update_book(bid, parse={'status': 'running', 'next': j['to'], 'total': total, 'rev': int(time.time() * 1000)},
                            hasText=True if got else b.get('hasText'))
        p.wait()
    finally:
        PARSE_PROC.pop(bid, None)
    if p.returncode not in (0, None) and not got:
        update_book(bid, parse={'status': 'error', 'error': 'Docling 解析失敗（代碼 %s）' % p.returncode})
        queue_proof(bid); return                                                       # 解析失敗：AI 直接看頁面圖讀
    with LOCK:
        pages = read_json(os.path.join(d, 'pages.json')) or []
        write_json(os.path.join(d, 'lost.json'), lost)
        b = update_book(bid, parse={'status': 'done', 'next': total, 'total': total, 'rev': int(time.time() * 1000)},
                        hasText=sum(len(page_text(pg)) for pg in pages) > 40)
    if b and b.get('noProofOnce'):                                     # 這次重新解析只改排版：不重新校對（不花額度）
        update_book(bid, noProofOnce=False)
    elif b and claude_path() and (read_json(CONFIG, {}) or {}).get('proof', True):
        with LOCK:                                                      # 重新解析過：整本重新校對（兩輪都重來）
            for fn in list(PROOF_FILES.values()) + ['read_ai.json']: write_json(os.path.join(d, fn), {})
        update_book(bid, proof={'status': 'queued', 'done': 0, 'total': total})  # 記下來：App 關掉再開也會接著校對
        PROOFQ.put(bid)                                                 # AI 逐頁對照原書校對
    elif b and b.get('scanned'): QQ.put(bid)                            # 沒有 Claude：至少檢查亂碼頁


def queue_proof(bid):
    """排進 AI 校對（沒文字的頁 AI 直接讀 → Haiku 逐頁 → Sonnet 抽查）。有 Claude 而且設定沒關掉才排"""
    if not claude_path() or not (read_json(CONFIG, {}) or {}).get('proof', True): return False
    b = find_book(load_index(), bid)
    if not b or (b.get('proof') or {}).get('status') in ('running', 'queued'): return False
    update_book(bid, proof={'status': 'queued', 'done': 0, 'total': b.get('pages') or 0}); PROOFQ.put(bid); return True


def start_parse(bid, restart=False):
    b = find_book(load_index(), bid)
    if not b: return None
    if DOCLING['status'] in ('unavailable', 'error'):
        b = update_book(bid, parse={'status': 'skipped', 'error': DOCLING.get('error') or 'Docling 不能用'})
        queue_proof(bid); return find_book(load_index(), bid) or b                  # 解析不了：AI 直接看頁面圖
    nxt = 0 if restart else int((b.get('parse') or {}).get('next') or 0)
    b = update_book(bid, parse={'status': 'queued', 'next': nxt, 'total': b.get('pages') or 0})
    parse_put(bid); return b


# ---------- AI：只在匯入時判斷一次書名、作者、分類、標籤、哪一頁是封面（使用者改過的欄位不蓋掉）----------
AIQ = queue.Queue()


def ai_meta(b, pages, cats):
    sample = ''.join('\n\n[PAGE %d]\n' % (i + 1) + '\n'.join(x for x in pg if is_text(x)) for i, pg in enumerate(pages[:5]))[:6000]
    prompt = ('Below is the beginning of a PDF the user imported (file name: %r), page by page. Identify the book/document. '
              'Reply with ONLY a JSON object: {"title": "...", "author": "... or empty", "category": "...", "tags": ["..", ".."], "coverPage": 0}. '
              'category must be exactly one of %s, or "" if none fits. tags: up to 3 short topic tags in Traditional Chinese. '
              'coverPage: the page number (1-5) that is the book\'s front cover (little text, mostly title/author), or 0 if none of them is.\n\n%s'
              % (b.get('file') or b['title'], json.dumps(cats, ensure_ascii=False), sample))
    return ai_json(prompt, dict, 180)


def render_cover(bid, page_no):
    """用 Docling 環境裡的 pypdfium2 把某一頁畫成封面圖（Docling 還沒裝好就跳過）"""
    if not os.path.exists(VPY): return False
    d = os.path.join(LIB, bid)
    code = ('import sys,pypdfium2 as P;d=P.PdfDocument(sys.argv[1]);p=d[int(sys.argv[2])-1];'
            's=480/p.get_width();p.render(scale=s).to_pil().convert("RGB").save(sys.argv[3],"JPEG",quality=84)')
    r = subprocess.run([VPY, '-c', code, os.path.join(d, 'book.pdf'), str(page_no), os.path.join(d, 'cover.jpg')], capture_output=True, timeout=120)
    return r.returncode == 0


def ai_worker():
    while True:
        bid = AIQ.get()
        wait_session()
        try: ai_job(bid)
        except Exception as e:
            update_book(bid, ai={'status': 'error', 'error': str(e)[:300]})


def ai_job(bid):
    with LOCK:
        ix = load_index(); b = find_book(ix, bid)
    if not b: return
    d = os.path.join(LIB, bid)
    pages = read_json(os.path.join(d, 'pages.json')) or []
    update_book(bid, ai={'status': 'running'})
    try: m = ai_meta(b, pages, ix['categories'])
    except RuntimeError as e:
        update_book(bid, ai={'status': 'error', 'error': str(e)[:300]}); return
    with LOCK:
        ix = load_index(); b = find_book(ix, bid)
        if not b: return
        ed = set(b.get('edited') or [])                     # 使用者自己改過的欄位：不蓋掉
        if 'title' not in ed and str(m.get('title') or '').strip(): b['title'] = str(m['title']).strip()[:300]
        if 'author' not in ed and str(m.get('author') or '').strip(): b['author'] = str(m['author']).strip()[:200]
        if 'category' not in ed and m.get('category') in ix['categories']: b['category'] = m['category']
        if 'tags' not in ed and isinstance(m.get('tags'), list): b['tags'] = [str(t).strip()[:40] for t in m['tags'][:3] if str(t).strip()]
        cp = m.get('coverPage'); cover_job = isinstance(cp, int) and 1 <= cp <= min(5, len(pages)) and not b.get('coverUser')
        b['ai'] = {'status': 'done'}; save_index(ix)
    if cover_job:
        DOCLING_READY.wait(900)
        if render_cover(bid, cp): update_book(bid, cover=True, coverV=int(time.time() * 1000))


def ai_clean_page(pg):
    """用 AI 整理一頁：圖表之間的每一段文字各自整理，圖表留在原位"""
    segs, cur = [], []
    for x in pg:
        if is_text(x): cur.append(x)
        else: segs.append(cur); segs.append(x); cur = []
    segs.append(cur)
    chunk = {str(i): s for i, s in enumerate(segs) if isinstance(s, list) and s}
    if not chunk: return pg
    prompt = ('These are paragraphs extracted from one page of a PDF book (JSON: key -> list of paragraphs; keys are separate text '
              'blocks around figures). The extraction may have broken paragraphs in the wrong places, hyphenated line-break words, '
              'running headers/footers, page numbers, OCR noise and stray figure labels. Return the cleaned list of paragraphs for each key: '
              'merge pieces that belong to one paragraph, split ones that were wrongly joined, rejoin hyphenated words, keep headings as '
              'their own paragraphs, remove headers/footers/page numbers/noise. Fix obvious OCR errors (e.g. "Ta king" -> "Taking"), but do '
              'NOT translate, summarize or reword anything else. Reply with ONLY a JSON object with the same keys.\n\n%s'
              % json.dumps(chunk, ensure_ascii=False))
    res = ai_json(prompt, dict, 420, kind='best')
    out = []
    for i, s in enumerate(segs):
        if not isinstance(s, list): out.append(s); continue
        v = res.get(str(i))
        if isinstance(v, list):
            v = [re.sub(r'\s+', ' ', str(x)).strip() for x in v if str(x).strip()]
            if len(re.sub(r'\s', '', ''.join(v))) >= len(re.sub(r'\s', '', ''.join(s))) * 0.6: s = v      # 少太多字：不信任
        out.extend(s)
    return out


class H(BaseHTTPRequestHandler):
    server_version = 'Readiverse/1.0'

    def log_message(self, fmt, *a): pass

    # --- 回應 ---
    def send(self, code, body=b'', ctype='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)): body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str): body = body.encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command != 'HEAD': self.wfile.write(body)

    def err(self, code, msg): self.send(code, {'error': msg})

    def body_bytes(self):
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n) if n else b''

    def body_json(self):
        try: return json.loads(self.body_bytes() or b'{}')
        except Exception: return {}

    def send_file(self, p, ctype):
        if not os.path.isfile(p): return self.err(404, 'not found')
        with open(p, 'rb') as f: data = f.read()
        self.send(200, data, ctype)

    # --- 路由 ---
    def route(self):
        u = urlparse(self.path); path = unquote(u.path); qs = parse_qs(u.query)
        parts = [x for x in path.split('/') if x]
        m = self.command
        try:
            if m == 'GET' and path in ('/', '/index.html'):
                return self.send_file(os.path.join(STATIC, 'index.html'), 'text/html; charset=utf-8')
            if m == 'GET' and parts[:1] == ['static'] and len(parts) == 2 and re.match(r'^[\w.\-]+$', parts[1]):
                ext = parts[1].rsplit('.', 1)[-1]
                ct = {'js': 'text/javascript', 'css': 'text/css', 'pdf': 'application/pdf', 'svg': 'image/svg+xml', 'png': 'image/png'}.get(ext, 'application/octet-stream')
                return self.send_file(os.path.join(STATIC, parts[1]), ct)
            if parts[:1] == ['books'] and len(parts) == 4 and parts[2] == 'figs' and m == 'GET' and re.match(r'^[\w.\-]+\.png$', parts[3]):
                return self.send_file(os.path.join(book_dir(parts[1]), 'figs', parts[3]), 'image/png')
            if parts[:1] == ['books'] and len(parts) == 3 and m == 'GET':
                fn = parts[2]
                if fn not in ('book.pdf', 'cover.jpg'): return self.err(404, 'not found')
                return self.send_file(os.path.join(book_dir(parts[1]), fn), 'application/pdf' if fn.endswith('pdf') else 'image/jpeg')
            if parts[:1] != ['api']: return self.err(404, 'not found')
            return self.api(m, parts[1:], qs)
        except ValueError as e: return self.err(400, str(e))
        except Exception as e:
            import traceback; traceback.print_exc()
            return self.err(500, str(e))

    def api(self, m, p, qs):
        if p == ['library'] and m == 'GET':
            with LOCK: return self.send(200, lib_view(load_index()))
        # ---------- 字典、單字收藏、句子收藏 ----------
        if p == ['meaning'] and m == 'GET':
            return self.send(200, rv_dict.lookup((qs.get('w') or [''])[0]))
        if p == ['explain'] and m == 'POST':
            j = self.body_json()
            try: return self.send(200, rv_dict.explain(str(j.get('w') or '')[:80], str(j.get('ctx') or '')))
            except RuntimeError as e: return self.err(502, str(e))
        if p == ['examples'] and m == 'GET':
            try: return self.send(200, {'ex': rv_dict.ai_examples((qs.get('w') or [''])[0][:80])})
            except RuntimeError as e: return self.err(502, str(e))
        if p == ['vocab'] and m == 'GET':
            return self.send(200, rv_dict.vocab_marks())
        if p[:1] == ['vocab'] and len(p) == 2 and m == 'POST':
            j = self.body_json(); act = p[1]
            if act == 'add':
                try: return self.send(200, rv_dict.vocab_add(j.get('w', ''), j.get('ctx', ''), j.get('title', ''), j.get('book', ''), j.get('page')))
                except ValueError as e: return self.err(400, str(e))
            k = rv_dict.wkey(j.get('k', ''))
            if act == 'del': rv_dict.vocab_del(k); return self.send(200, {'ok': True})
            if act == 'review':
                v = next((x for x in rv_dict.vocab() if x['k'] == k), None)
                if not v: return self.err(404, '沒有這個字')
                return self.send(200, rv_dict.vocab_update(k, forgot=(v.get('forgot') or 0) + 1, forgotT=int(time.time() * 1000)))
            if act == 'note': return self.send(200, rv_dict.vocab_update(k, note=str(j.get('note') or '')[:2000]) or {})
        if p == ['phrases'] and m == 'GET':
            return self.send(200, rv_dict.phrases())
        if p[:1] == ['phrase'] and len(p) == 2 and m == 'POST':
            j = self.body_json(); act = p[1]
            if act == 'add':
                try: return self.send(200, rv_dict.phrase_add(j.get('text', ''), j.get('ctx', ''), j.get('title', ''), j.get('book', ''), j.get('page')))
                except ValueError as e: return self.err(400, str(e))
            if act == 'del': rv_dict.phrase_del(j.get('id')); return self.send(200, {'ok': True})
            if act == 'retry': rv_dict.phrase_retry(j.get('id')); return self.send(200, {'ok': True})
        if p == ['ielts-import'] and m == 'GET':
            return self.send(200, rv_dict.ielts_info())
        if p == ['ielts-import'] and m == 'POST':
            try: return self.send(200, rv_dict.ielts_import())
            except ValueError as e: return self.err(404, str(e))
        if p == ['update'] and m == 'GET':
            if upd_can() and not UPD['stage']: upd_check(force=(qs.get('force') or [''])[0] == '1')
            return self.send(200, upd_info())
        if p == ['update'] and m == 'POST':
            with UPD_LOCK:
                if not upd_can(): return self.err(400, '只有下載安裝的 Readiverse 可以自動更新')
                if UPD['stage'] and time.time() - UPD.get('ts', 0) > 90: UPD['stage'] = ''      # 上一次卡住了：重來
                if not UPD['stage']:
                    upd_check(force=True)
                    if not upd_info()['available']: return self.send(200, upd_info())
                    UPD.update(stage='download', ts=time.time())
                    threading.Thread(target=upd_run, daemon=True).start()
            return self.send(200, upd_info())
        if p == ['caps'] and m == 'GET':
            return self.send(200, {'docling': DOCLING, 'claude': bool(claude_path()), 'proof': (read_json(CONFIG, {}) or {}).get('proof', True), 'models': AI_MODELS, 'aiModels': {k: ai_model(k) for k in AI_DEFAULT}})
        if p == ['ai', 'auth'] and m == 'GET':
            return self.send(200, ai_auth())
        if p == ['ai', 'login'] and m == 'POST':
            return self.send(200, login_start())
        if p == ['ai', 'login'] and m == 'GET':
            return self.send(200, login_state())
        if p == ['ai', 'login', 'code'] and m == 'POST':
            return self.send(200, login_code(self.body_json().get('code', '')))
        if p == ['ai', 'login', 'cancel'] and m == 'POST':
            login_stop(); LOGIN['state'] = ''; return self.send(200, login_state())
        if p == ['ai', 'test'] and m == 'POST':
            return self.send(200, ai_test())
        if p == ['ai', 'proof'] and m == 'PUT':      # 匯入後要不要 AI 校對
            cfg = read_json(CONFIG, {}) or {}; cfg['proof'] = bool(self.body_json().get('on')); write_json(CONFIG, cfg)
            return self.send(200, {'proof': cfg['proof']})
        if p == ['ai', 'models'] and m == 'PUT':
            j = self.body_json(); cfg = read_json(CONFIG, {}) or {}
            cfg['aiModels'] = {k: j[k] for k in AI_DEFAULT if j.get(k) in [x[0] for x in AI_MODELS]}
            write_json(CONFIG, cfg)
            return self.send(200, {k: ai_model(k) for k in AI_DEFAULT})
        if p == ['docling'] and m == 'POST':          # 重新試著安裝 Docling
            if DOCLING['status'] not in ('installing', 'ready'): threading.Thread(target=docling_setup, daemon=True).start()
            return self.send(200, {'docling': DOCLING})
        if p == ['storage'] and m == 'GET':
            return self.send(200, storage_options())
        if p == ['storage'] and m == 'POST':
            target = str(self.body_json().get('path') or '')
            if target not in [o['path'] for o in storage_options()]: return self.err(400, '不能放在這裡')
            try: move_library(target)
            except OSError as e: return self.err(500, '搬移失敗：%s' % e)
            return self.send(200, lib_view(load_index()))
        if p == ['categories'] and m == 'PUT':
            j = self.body_json()
            cats = list(dict.fromkeys(str(c).strip()[:40] for c in (j.get('categories') or []) if str(c).strip()))
            ren = j.get('rename') or {}
            with LOCK:
                ix = load_index(); old = set(ix['categories']); ix['categories'] = cats
                for b in ix['books']:
                    c = b.get('category')
                    if c in ren and ren[c] in cats: b['category'] = ren[c]          # 分類改名：書跟著走
                    elif c and c in old and c not in cats: b['category'] = ''       # 刪掉的分類：書改成未分類
                save_index(ix)
                return self.send(200, lib_view(ix))
        if p == ['profile'] and m == 'PUT':
            j = self.body_json()
            with LOCK:
                ix = load_index(); ix['profile'] = {'name': str(j.get('name') or '').strip()[:40]}
                save_index(ix)
            return self.send(200, ix['profile'])
        if p == ['session'] and m == 'GET':             # 開機檢查：這台能不能用這個書庫
            return self.send(200, session_info())
        if p == ['session'] and m == 'POST':            # 重新檢查／強制在這台用
            j = self.body_json()
            if j.get('quit'): session_release(); threading.Timer(0.5, lambda: os._exit(0)).start(); return self.send(200, {'ok': True})
            return self.send(200, session_check(force=bool(j.get('force'))))
        if p == ['notes'] and m == 'GET':               # 首頁「我的筆記」
            return self.send(200, all_notes())
        if p == ['ask'] and m == 'POST':                # 選取文字問 AI
            try: return self.send(200, ask_ai(self.body_json()))
            except ValueError as e: return self.err(400, str(e))
            except RuntimeError as e: return self.err(500, str(e))
        if p == ['upload'] and m == 'POST':
            data = self.body_bytes()
            if not data.startswith(b'%PDF'): return self.err(400, '這不是 PDF 檔')
            name = os.path.basename((qs.get('name') or ['未命名'])[0])
            title = re.sub(r'\.pdf$', '', name, flags=re.I).strip() or '未命名'
            bid = uuid.uuid4().hex[:12]
            d = book_dir(bid); os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, 'book.pdf'), 'wb') as f: f.write(data)
            meta = {'id': bid, 'title': title, 'autoTitle': title, 'file': name, 'author': '', 'category': (qs.get('category') or [''])[0][:40], 'tags': [],
                    'status': 'unread', 'added': int(time.time() * 1000), 'opened': 0, 'pages': 0, 'lastPage': 0,
                    'favorite': False, 'hasText': True, 'size': len(data), 'notes': 0, 'marks': 0, 'cover': False}
            with LOCK:
                ix = load_index(); ix['books'].insert(0, meta); save_index(ix)
            return self.send(200, meta)
        if len(p) >= 2 and p[0] == 'books':
            bid = p[1]; d = book_dir(bid); sub = p[2] if len(p) > 2 else ''
            with LOCK:
                ix = load_index(); b = find_book(ix, bid)
            if not b: return self.err(404, '找不到這本書')
            if sub == '' and m == 'GET':
                return self.send(200, {'meta': b, 'pages': read_json(os.path.join(d, 'pages.json'), None),
                                       'state': read_json(os.path.join(d, 'state.json'), {}) or {}})
            if sub == 'pages' and m == 'GET':
                return self.send(200, {'meta': b, 'pages': read_json(os.path.join(d, 'pages.json'), None)})
            if sub == '' and m == 'PATCH':
                body = self.body_json(); upd = clean_meta(body)
                with LOCK:
                    ix = load_index(); b = find_book(ix, bid)
                    if body.get('byUser'):                  # 使用者在「詳細資料」改過的欄位：AI 之後不再蓋掉
                        ed = set(b.get('edited') or [])
                        for k in ('title', 'author', 'category', 'tags'):
                            if k in upd and upd[k] != b.get(k): ed.add(k)
                        b['edited'] = sorted(ed)
                    if 'lastPage' in upd: b['opened'] = int(time.time() * 1000)
                    if upd.get('lastPage') and b.get('status') == 'unread' and 'status' not in upd: upd['status'] = 'reading'
                    b.update(upd); save_index(ix)
                return self.send(200, b)
            if sub == '' and m == 'DELETE':
                pr = PARSE_PROC.get(bid)
                if pr:
                    try: pr.kill()
                    except Exception: pass
                with LOCK:
                    ix = load_index(); ix['books'] = [x for x in ix['books'] if x['id'] != bid]; save_index(ix)
                    shutil.rmtree(d, ignore_errors=True)
                return self.send(200, {'ok': True})
            if sub == 'pages' and m == 'PUT':
                pages = self.body_json().get('pages')
                if not isinstance(pages, list): return self.err(400, 'pages 格式不對')
                pages = [[str(x) for x in pg] if isinstance(pg, list) else [] for pg in pages]
                write_json(os.path.join(d, 'pages_raw.json'), pages)
                pages = clean_pages(pages)
                write_json(os.path.join(d, 'pages.json'), pages)
                b = update_book(bid, pages=len(pages), hasText=sum(len(page_text(pg)) for pg in pages) > 40)
                return self.send(200, b)
            if sub == 'parse' and m == 'POST':       # 用 Docling 解析（匯入後自動叫；「詳細資料」可以重新解析）
                if (b.get('parse') or {}).get('status') in ('running', 'queued') and not self.body_json().get('restart'): return self.send(200, b)
                return self.send(200, start_parse(bid, restart=True))
            if sub == 'ai' and m == 'POST':          # AI 判斷書名、作者、分類、標籤、封面
                if (b.get('ai') or {}).get('status') in ('running', 'queued'): return self.send(200, b)
                if not b.get('hasText'): return self.err(400, '這本 PDF 抽不出文字，AI 沒辦法判斷')
                b = update_book(bid, ai={'status': 'queued'}); AIQ.put(bid)
                return self.send(200, b)
            if sub == 'proof' and m == 'POST':       # 重新校對整本
                if self.body_json().get('restart'):
                    for fn in list(PROOF_FILES.values()) + ['read_ai.json']: write_json(os.path.join(d, fn), {})
                b = update_book(bid, proof={'status': 'queued', 'done': 0, 'total': b.get('pages') or 0}); PROOFQ.put(bid)
                return self.send(200, b)
            if sub == 'ai-page' and m == 'POST':     # 用 AI 整理某一頁
                pno = int(self.body_json().get('page') or 0)
                with LOCK:
                    pages = read_json(os.path.join(d, 'pages.json')) or []
                    if not (0 <= pno < len(pages)): return self.err(400, '沒有這一頁')
                    if pno in used_pages(read_json(os.path.join(d, 'state.json'), {})): return self.err(409, '這一頁已經有螢光筆、筆記或翻譯，整理後位置會跑掉')
                    pg = pages[pno]
                try: new = ai_clean_page(pg)
                except RuntimeError as e: return self.err(502, str(e))
                with LOCK:
                    pages = read_json(os.path.join(d, 'pages.json')) or []
                    if pno in used_pages(read_json(os.path.join(d, 'state.json'), {})): return self.err(409, '這一頁剛剛加了標記，沒有套用')
                    pages[pno] = new; write_json(os.path.join(d, 'pages.json'), pages)
                    bb = [i for i in (b.get('ocrBad') or []) if i != pno or garble(new)[0] > QUALITY_THR]
                    update_book(bid, ocrBad=bb)
                return self.send(200, {'page': new, 'ocrBad': bb})
            if sub == 'cover' and m == 'PUT':
                data = self.body_bytes()
                if data[:2] != b'\xff\xd8': return self.err(400, '封面要是 JPEG')
                with open(os.path.join(d, 'cover.jpg'), 'wb') as f: f.write(data)
                return self.send(200, update_book(bid, cover=True, coverV=int(time.time() * 1000), coverUser=bool(qs.get('user'))))
            if sub == 'cover' and m == 'DELETE':           # 不用圖片，改用設計好的封面（書名＋作者）
                try: os.remove(os.path.join(d, 'cover.jpg'))
                except OSError: pass
                return self.send(200, update_book(bid, cover=False, coverV=int(time.time() * 1000), coverUser=True))
            if sub == 'state' and m == 'PUT':
                st = self.body_json()
                if not isinstance(st, dict): return self.err(400, 'state 格式不對')
                notes = sum(1 for n in (st.get('notes') or {}).values() if str((n or {}).get('text') or '').strip()) \
                    + sum(1 for t in (st.get('pageNotes') or {}).values() if str(t or '').strip())\
                    + sum(1 for t in (st.get('paraNotes') or {}).values() if str(t or '').strip())
                marks = sum(len(v) for v in (st.get('marks') or {}).values() if isinstance(v, list))
                with LOCK:
                    write_json(os.path.join(d, 'state.json'), st)
                    update_book(bid, notes=notes, marks=marks)
                return self.send(200, {'ok': True, 'notes': notes, 'marks': marks})
            if sub == 'translate' and m == 'POST':
                j = self.body_json(); paras = [str(x) for x in (j.get('paras') or []) if isinstance(x, str)]
                if not paras: return self.err(400, '這一頁沒有文字')
                try: zh = translate(paras)
                except RuntimeError as e: return self.err(502, str(e).replace('AI 失敗', '翻譯失敗'))
                return self.send(200, {'zh': zh})
        return self.err(404, 'not found')

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = route

# ---------- 版本與自動更新（跟雅思練習室一樣；只有下載的 exe / app 版會自動更新）----------
UPD_REPO = 'sleepersusu/readiverse'
EXE = os.environ.get('READIVERSE_EXE', '')          # 打包版由 app.py 設定：執行檔路徑


def _read_ver():
    for d in (ROOT, os.path.dirname(EXE) if EXE else ''):
        try:
            with open(os.path.join(d, 'VERSION'), encoding='utf-8-sig') as f: return f.read().strip()
        except Exception: pass
    return ''


VERSION = _read_ver()                                # 例如 v0.2.0；自己電腦打包的是 v0.2.0-dev（不自動更新）
UPD = {'stage': '', 'got': 0, 'total': 0, 'error': '', 'checked': 0, 'latest': '', 'asset': '', 'size': 0, 'notes': [], 'ts': 0}
UPD_LOCK = threading.Lock()


def _vt(v):
    try: return tuple(int(x) for x in str(v).lstrip('vV').split('.'))
    except Exception: return None


def _upd_asset():
    if WIN: return 'Readiverse-Setup.exe'            # Windows：下載安裝程式，安靜模式安裝
    if sys.platform == 'darwin': return 'Readiverse-mac.zip'
    return ''


def _mac_app():
    p = os.path.abspath(EXE)
    for _ in range(3): p = os.path.dirname(p)
    return p if p.endswith('.app') else ''


def upd_can():
    if not EXE or not _upd_asset() or _vt(VERSION) is None: return False     # 原始碼版、自己打包的開發版：不自動更新
    if sys.platform == 'darwin':
        a = _mac_app()
        return bool(a) and '/AppTranslocation/' not in a   # 沒拖到「應用程式」、直接從下載執行：macOS 放在唯讀的暫存位置，換不了
    return True


def _ssl_ctx():
    """HTTPS 憑證：打包版的 Python 不一定找得到系統的憑證，有 certifi 就用它"""
    import ssl
    try:
        import certifi; return ssl.create_default_context(cafile=certifi.where())
    except Exception: return ssl.create_default_context()


def _open(url, timeout=30, accept=None):
    req = urllib.request.Request(url, headers={'User-Agent': 'readiverse', **({'Accept': accept} if accept else {})})
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx())


def _gh(url):
    try:
        with _open(url, 10, 'application/vnd.github+json') as r: return json.loads(r.read())
    except Exception as e:
        try:                                          # Python 連不上（例如憑證）：改用系統內建的 curl
            out = subprocess.run(['curl', '-sfL', '--max-time', '15', '-H', 'User-Agent: readiverse', url], capture_output=True, timeout=20).stdout
            if out: return json.loads(out.decode('utf-8'))
        except Exception: pass
        raise e


def upd_check(force=False):
    s = UPD
    if not upd_can(): return
    if not force and time.time() - s['checked'] < 600: return
    s['checked'] = time.time()
    try:
        rels = [r for r in _gh('https://api.github.com/repos/%s/releases?per_page=30' % UPD_REPO) if not r.get('draft') and not r.get('prerelease')]
        name = _upd_asset(); best = None
        for r in rels:                                # 最新的、而且有這個平台安裝檔的版本
            a = next((x for x in r.get('assets', []) if x.get('name') == name), None)
            if a and _vt(r.get('tag_name')) and (not best or _vt(r['tag_name']) > _vt(best[0]['tag_name'])): best = (r, a)
        if not best: s.update(latest='', asset='', size=0, notes=[]); return
        r, a = best; cur = _vt(VERSION)
        s.update(latest=r['tag_name'], asset=a['browser_download_url'], size=a.get('size', 0))
        notes = []                                    # 跳過好幾版才更新：中間每一版的更新內容都列出來
        for rr in sorted(rels, key=lambda x: _vt(x.get('tag_name')) or (), reverse=True):
            tv = _vt(rr.get('tag_name'))
            if tv and (cur is None or tv > cur):
                notes += [l[2:].strip() for l in str(rr.get('body') or '').splitlines() if l.startswith('- ')]
        s['notes'] = list(dict.fromkeys(n for n in notes if n))[:60]
        s['checkError'] = ''
    except Exception as e:
        print('檢查更新失敗：', e); s['checkError'] = '連不上 GitHub：%s' % str(e)[:160]


def upd_info():
    s = UPD; cur, lat = _vt(VERSION), _vt(s['latest'])
    avail = bool(s['asset'] and lat and cur and lat > cur)
    return {'can': upd_can(), 'current': VERSION, 'latest': s['latest'], 'available': avail, 'stage': s['stage'],
            'pct': int(s['got'] * 100 / s['total']) if s['total'] else 0, 'error': s['error'], 'notes': s['notes'],
            'checkError': s.get('checkError', ''),
            'repo': UPD_REPO}


def upd_loop():
    """App 開著的時候：每 30 分鐘看一次有沒有新版"""
    time.sleep(15)
    while True:
        try:
            if upd_can() and not UPD['stage']: upd_check(force=True)
        except Exception as e: print('檢查更新失敗：', e)
        time.sleep(1800)


def upd_log(m):
    try:
        with open(os.path.join(HOME_DIR, 'update.log'), 'a', encoding='utf-8') as f: f.write(time.strftime('%Y-%m-%d %H:%M:%S') + '  ' + m + '\n')
    except Exception: pass


def upd_run():
    """下載新版 → 交給安裝程式／小程式：等 Readiverse 關掉、換成新版、再打開 → 關掉 Readiverse
    書庫和設定都不在程式資料夾裡，更新不會動到；做到一半的解析、校對，新版打開會接著做"""
    import tempfile, zipfile
    s = UPD
    try:
        upd_log('從 %s 更新到 %s：開始下載' % (VERSION or '?', s.get('latest') or '?'))
        os.makedirs(HOME_DIR, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix='readiverse-update-'); z = os.path.join(tmp, 'new.bin')
        for attempt in range(3):                       # 網路斷掉就重新下載（最多 3 次），沒下載完整絕不安裝
            s.update(stage='download', got=0, total=s['size'] or 0, error='', ts=time.time())
            try:
                with _open(s['asset'], 30) as r, open(z, 'wb') as f:
                    s['total'] = int(r.headers.get('Content-Length') or s['total'] or 0)
                    while True:
                        b = r.read(1 << 16)
                        if not b: break
                        f.write(b); s['got'] += len(b); s['ts'] = time.time()
                if s['total'] and s['got'] != s['total']: raise RuntimeError('只下載到 %d / %d bytes' % (s['got'], s['total']))
                if not WIN: zipfile.ZipFile(z).testzip()
                break
            except Exception as e:
                upd_log('下載失敗（第 %d 次）：%s' % (attempt + 1, e))
                if attempt == 2: raise RuntimeError('網路不穩，新版沒下載完整，請稍後再按一次更新')
                time.sleep(3)
        upd_log('下載完成（%d bytes）' % s['got'])
        s['stage'] = 'install'; pid = os.getpid()
        if WIN:
            setup = os.path.join(tmp, 'Readiverse-Setup.exe'); os.replace(z, setup)
            subprocess.Popen([setup, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CURRENTUSER', '/WAITPID=%d' % pid,
                              '/LOG=' + os.path.join(HOME_DIR, 'install.log')], cwd=tmp,
                             creationflags=0x00000008 | 0x00000200, close_fds=True)     # 跟 Readiverse 分開跑（DETACHED）
        else:
            app = _mac_app(); sh = os.path.join(tmp, 'update.sh'); os.replace(z, os.path.join(tmp, 'new.zip'))
            qq = lambda x: "'" + x.replace("'", "'\\''") + "'"
            with open(sh, 'w', encoding='utf-8') as f:
                f.write("""#!/bin/bash
exec >> %s 2>&1
echo "$(date '+%%F %%T')  開始更新到 %s"
while kill -0 %d 2>/dev/null; do sleep 1; done
sleep 1
APP=%s; TMP=%s
ditto -x -k "$TMP/new.zip" "$TMP/new" || { open "$APP"; exit 1; }
NEW=$(ls -d "$TMP"/new/*.app | head -1); [ -d "$NEW" ] || { open "$APP"; exit 1; }
rm -rf "$APP.old"; mv "$APP" "$APP.old" || { open "$APP"; exit 1; }
if ditto "$NEW" "$APP"; then rm -rf "$APP.old"; else rm -rf "$APP"; mv "$APP.old" "$APP"; fi
xattr -cr "$APP" 2>/dev/null
open "$APP" && echo "$(date '+%%F %%T')  已重新打開"
rm -rf "$TMP"
""" % (qq(os.path.join(HOME_DIR, 'update.log')), s.get('latest'), pid, qq(app), qq(tmp)))
            os.chmod(sh, 0o755)
            subprocess.Popen(['/bin/bash', sh], start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        s['stage'] = 'restart'; s['ts'] = time.time()
        upd_log('新版準備好了，Readiverse 關掉後會換成新版並重新打開')
        def bye():
            time.sleep(1.5)
            session_release()
            with LOCK: os._exit(0)                     # 等正在寫的檔案寫完再關
        threading.Thread(target=bye, daemon=True).start()
    except Exception as e:
        upd_log('更新失敗：%s' % e)
        s.update(stage='', error='更新失敗：%s' % e)



# ---------- 一次只能一台電腦用同一個書庫（跟雅思練習室一樣）：書庫放在雲端時，避免兩台同時改、互相蓋掉 ----------
import socket
DEVICE = (socket.gethostname() or '這台電腦').split('.')[0]


def _device_id():
    """每台電腦自己的編號（放在使用者資料夾，不會跟著書庫同步），用來分辨是不是同一台"""
    p = os.path.join(os.path.expanduser('~'), '.readiverse', 'device-id')
    try:
        with open(p, encoding='utf-8') as f:
            v = f.read().strip()
            if v: return v
    except OSError: pass
    v = uuid.uuid4().hex
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f: f.write(v)
    except OSError: pass
    return v


DEVICE_ID = _device_id()
LOCK_FRESH = 6 * 60 * 1000          # 別台電腦 6 分鐘內有更新「使用中」記號 = 還開著（開著的每 2 分鐘更新一次）
SESS = {'state': 'checking', 'other': None}   # checking / ok / blocked（別台正在用）/ taken（別台強制接手）


def _lock_path(): return os.path.join(LIB, '_使用中.json')


def _lock_write():
    write_json(_lock_path(), {'device': DEVICE, 'id': DEVICE_ID, 't': int(time.time() * 1000)})


def session_check(force=False):
    """打開時：看有沒有別台電腦正在用這個書庫。沒有（或強制）就掛上「使用中」"""
    lk = read_json(_lock_path(), {}) or {}
    busy = lk if lk.get('id') and lk.get('id') != DEVICE_ID and time.time() * 1000 - (lk.get('t') or 0) < LOCK_FRESH else None
    if busy and not force:
        SESS.update(state='blocked', other={'device': busy.get('device') or '另一台電腦', 't': busy.get('t') or 0})
    else:
        try: _lock_write()
        except OSError: pass
        SESS.update(state='ok', other=None)
    return session_info()


def session_info():
    return {'state': SESS['state'], 'other': SESS['other'], 'device': DEVICE, 'lib': LIB,
            'cloud': next((x['kind'] for x in storage_options() if x['current']), 'local') != 'local'}


def session_loop():
    """開著的時候每 2 分鐘更新「使用中」；發現別台強制接手了，就停下來（不再寫書庫）"""
    while True:
        time.sleep(120)
        if SESS['state'] != 'ok': continue
        lk = read_json(_lock_path(), {}) or {}
        if lk.get('id') and lk.get('id') != DEVICE_ID:
            SESS.update(state='taken', other={'device': lk.get('device') or '另一台電腦', 't': lk.get('t') or 0}); continue
        try: _lock_write()
        except OSError: pass


def session_release():
    """關掉 Readiverse：拿掉「使用中」記號，別台電腦就可以直接打開"""
    try:
        lk = read_json(_lock_path(), {}) or {}
        if lk.get('id') == DEVICE_ID: write_json(_lock_path(), {'device': '', 't': 0, 'closed': DEVICE, 'closedT': int(time.time() * 1000)})
    except Exception: pass


def wait_session():
    """背景工作（解析、校對、AI）：這台拿到書庫才做；別台在用時就等"""
    while SESS['state'] != 'ok': time.sleep(2)


def main():
    if LIB == LOCAL_LIB and not os.path.exists(LOCAL_LIB) and os.path.isfile(os.path.join(OLD_LIB, '_index.json')):
        try: shutil.move(OLD_LIB, LOCAL_LIB); print('書庫搬到 %s' % LOCAL_LIB)
        except OSError as e: print('書庫搬家失敗：%s' % e)
    os.makedirs(LIB, exist_ok=True)
    session_check()                                   # 別台電腦正在用：先擋住（背景工作也先等）
    threading.Thread(target=session_loop, daemon=True).start()
    threading.Thread(target=ai_worker, daemon=True).start()
    rv_dict.init(lambda: LIB, ai_json)
    threading.Thread(target=parse_worker, daemon=True).start()
    threading.Thread(target=quality_worker, daemon=True).start()
    for _ in range(2): threading.Thread(target=proof_worker, daemon=True).start()   # 兩本書同時校對（每本同時 2 頁）
    threading.Thread(target=docling_setup, daemon=True).start()
    threading.Thread(target=upd_loop, daemon=True).start()
    with LOCK:                                   # 上次沒做完的：接著做
        for b in load_index()['books']:
            if (b.get('ai') or {}).get('status') in ('running', 'queued'): AIQ.put(b['id'])
            if (b.get('parse') or {}).get('status') in ('running', 'queued'): parse_put(b['id'])
            elif (b.get('proof') or {}).get('status') in ('running', 'queued'): PROOFQ.put(b['id'])
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
    url = 'http://localhost:%d' % PORT
    print('Readiverse 已啟動：%s\n書庫：%s\n要關掉請按 Ctrl+C' % (url, LIB))
    if '--no-browser' not in sys.argv: threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: print('\n已關閉')
    finally: session_release()


if __name__ == '__main__':
    main()
