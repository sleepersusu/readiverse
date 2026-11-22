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
import json, os, re, shutil, subprocess, sys, threading, time, uuid, webbrowser, queue, glob
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
import rv_dict

ROOT = os.path.dirname(os.path.abspath(__file__))


def _fix_path():
    """雙擊 App 打開時，macOS 只給很短的 PATH（找不到 claude、node、uv）：補上使用者登入時的 PATH"""
    if sys.platform.startswith('win'): return
    extra = []
    try:
        sh = os.environ.get('SHELL') or '/bin/zsh'
        r = subprocess.run([sh, '-lic', 'printf "__P__%s__P__" "$PATH"'], capture_output=True, text=True, timeout=8, stdin=subprocess.DEVNULL)
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
AI_DEFAULT = {'best': 'claude-sonnet-5-5', 'fast': 'claude-haiku-5-5'}       # best：校對、整理頁面；fast：翻譯、書名、解釋


def ai_model(kind='fast'):
    m = ((read_json(CONFIG, {}) or {}).get('aiModels') or {}).get(kind)
    return m if m in [x[0] for x in AI_MODELS] else AI_DEFAULT[kind]


def claude_path():
    c = shutil.which('claude') or os.path.expanduser('~/.local/bin/claude')
    return c if os.path.exists(c) else None


def ai_cmds(prompt, model=None):
    """只用 Claude（電腦上登入的 Claude Code）"""
    claude = claude_path()
    if not claude: return []
    return [('Claude Code', [claude, '-p', '--model', model or ai_model('fast'), prompt])]


def ai_auth():
    """Claude 登入狀態（claude auth status）"""
    c = claude_path()
    if not c: return {'installed': False, 'loggedIn': False}
    try:
        r = subprocess.run([c, 'auth', 'status', '--json'], capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
        j = json.loads(r.stdout or '{}')
    except Exception: j = {}
    return {'installed': True, 'loggedIn': bool(j.get('loggedIn')), 'expires': j.get('loginExpiresAt') or '', 'method': j.get('authMethod') or ''}


LOGIN = {'state': '', 'url': '', 'error': '', 'out': '', 'fd': None, 'proc': None}


def login_start():
    """在背景跑 claude auth login（看不見的終端機）：它會自己打開瀏覽器的授權頁；網址也抓下來，App 裡可以再按一次打開"""
    c = claude_path()
    if not c: return {'state': 'error', 'error': '這台電腦沒有 Claude Code'}
    login_stop()
    import pty, select
    m, s_ = pty.openpty()
    p = subprocess.Popen([c, 'auth', 'login'], stdin=s_, stdout=s_, stderr=s_, close_fds=True, start_new_session=True)
    os.close(s_)
    LOGIN.update(state='waiting', url='', error='', out='', fd=m, proc=p)
    def pump():
        buf = b''
        while LOGIN['proc'] is p:
            try:
                r, _, _ = select.select([m], [], [], 0.5)
                if r: buf += os.read(m, 4096)
            except OSError: break
            txt = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]|\x1b8;;[^\x1b]*\x1b', '', buf.decode('utf-8', 'ignore'))
            LOGIN['out'] = txt[-2000:]
            u = re.findall(r'https://[^\s\x1b\x07\]]+', buf.decode('utf-8', 'ignore'))
            if u and not LOGIN['url']: LOGIN['url'] = u[0]
            if 'Login successful' in txt or 'successfully' in txt.lower(): LOGIN['state'] = 'done'; break
            if p.poll() is not None:
                if LOGIN['state'] == 'waiting': LOGIN.update(state='error', error=(txt.strip().splitlines() or ['登入失敗'])[-1][:200])
                break
        try: os.close(m)
        except OSError: pass
    threading.Thread(target=pump, daemon=True).start()
    return login_state()


def login_code(code):
    """授權頁給了一串代碼：貼回登入程式"""
    if LOGIN.get('fd') is None or LOGIN['state'] != 'waiting': return login_state()
    try: os.write(LOGIN['fd'], (str(code).strip() + '\r').encode())
    except OSError as e: LOGIN.update(state='error', error=str(e))
    return login_state()


def login_stop():
    p = LOGIN.get('proc')
    if p and p.poll() is None:
        try: os.killpg(p.pid, 15)
        except Exception: pass
    LOGIN.update(proc=None, fd=None)


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
            r = subprocess.run([c, '-p', '--model', m, 'Reply with exactly: OK'], capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL, cwd=os.path.expanduser('~'))
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
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=os.path.expanduser('~'))
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
                x = re.sub(r'^[•·*«»~&§¶|]+\s*', '', x)                      # 花紋、項目符號被辨識成「•」「*」「«」
                x = re.sub(r'^[,.;:]+\s+', '', x)                            # 段落開頭多出來的標點
                x = re.sub(r'^(?:[-–]|[a-z])\s+(?=[A-Z"“‘])', '', x)         # 章節開頭的裝飾字被辨識成「-」或一個小寫字母
                x = re.sub(r'^[-–]\s+(?=[a-z])', '', x)                       # 「- twins, and they…」：換行的連字號跑到段落開頭
                x = re.sub(r'(\d)- (?=[A-Z])', r'\1. ', x)                       # 「summer of 1994- Looking」：句號被認成「-」
                x = re.sub(r'(?<=[.!?"”’)])\s?(\d{1,3}(?:,\s?\d{1,3})*)(?=\s+[A-Z"“‘]|\s*$)', lambda m: m.group(0) if NOT_FN.search(m.string[:m.start()]) else m.group(1).replace(' ', '').translate(SUPS), x)   # 註腳編號：書上的上標 ¹⁹ 被讀成一般數字「baseline. 19」→ 改回上標「baseline.¹⁹」
                if not x or re.fullmatch(r'(page\s*)?[\divxlc]{1,5}', x, re.I): continue      # 只有頁碼
                if re.fullmatch(r'[A-HJ-Za-z]{1,2}|[^\w\s]{1,3}', x): continue           # 裝飾字、單獨一兩個字母（「N」）
            out.append(x)
        pages[p] = out
    # 2) 頁首頁尾：很多頁的開頭／結尾都出現的短句（書名、章名、「78 EMOTIONALLY CHARGED」）
    cnt = {}
    for pg in pages:
        T = [x for x in pg if is_text(x)]
        for k in {_hkey(x) for x in T[:2] + T[-2:] if _headish(x)}:
            cnt[k] = cnt.get(k, 0) + 1
    heads = {k for k, c in cnt.items() if c >= 3 and len(k) >= 6}
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
PROOF_PROMPT = ("Read the image file %s. It is one page of a printed book.\n"
    "Below is the text we extracted. \"prev_tail\" is the end of the previous page's last paragraph (this page may start in the middle of it). "
    "\"paras\" are the text paragraphs on this page (figures and tables are handled separately; ignore their contents).\n"
    "Proofread word by word against the image, IN READING ORDER. Report ONLY real differences in the body text: missing words or sentences, misread words, "
    "words or sentences in the wrong place or order (for example a word that belongs at the end of prev_tail but was put at the start of paragraph 0 — "
    "fix it by inserting it in the right place and replacing the misplaced copy with an empty string), "
    "wrong punctuation (em dashes, quotes, apostrophes). Footnote reference numbers should be superscript digits (like ¹⁹) right after the word or punctuation.\n"
    "Ignore the running header and page number, line-end hyphenation, indentation and paragraph spacing.\n"
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


def proof_page(bid, pno, imgdir):
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
    payload = json.dumps({'prev_tail': prev[-200:], 'paras': paras}, ensure_ascii=False)
    c = claude_path()
    if not c: raise RuntimeError('找不到 Claude Code')
    r = subprocess.run([c, '-p', '--model', ai_model('best'), '--allowedTools', 'Read', '--add-dir', imgdir, '--', PROOF_PROMPT % (img, payload)],
                       capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL, cwd=os.path.expanduser('~'))
    m = re.search(r'\{[\s\S]*\}', r.stdout or '')
    if r.returncode != 0 or not m:
        err = ((r.stderr or r.stdout or '').strip().splitlines() or ['沒有回應'])[-1][:200]
        if re.search(r'auth|login|expired', err, re.I): raise RuntimeError('Claude 沒有登入：' + err)
        return {'ok': False, 'error': err}
    try: j = json.loads(m.group(0))
    except ValueError: return {'ok': False, 'error': 'AI 回傳的格式不對'}
    applied, failed = [], []
    with LOCK:
        pages = read_json(os.path.join(d, 'pages.json')) or []
        frozen = used_pages(read_json(os.path.join(d, 'state.json'), {}))
        def target(where):
            if where == 'prev_tail':
                if pno == 0 or (pno - 1) in frozen: return None
                pg = pages[pno - 1]; idx = max([i for i, x in enumerate(pg) if is_text(x)] or [-1])
                return (pno - 1, idx) if idx >= 0 else None
            if pno in frozen: return None
            try: k = int(where)
            except (TypeError, ValueError): return None
            T = [i for i, x in enumerate(pages[pno]) if is_text(x)]
            return (pno, T[k]) if 0 <= k < len(T) else None
        for f in j.get('fixes') or []:
            t, find, rep_ = target(f.get('where')), str(f.get('find') or ''), str(f.get('replace') or '')
            if not t or not find or len(rep_) > len(find) + 300: failed.append(f); continue
            if not rep_ and len(find) > 60: failed.append(f); continue      # 不准一次刪掉一大段
            txt = pages[t[0]][t[1]]
            if txt.count(find) != 1: failed.append(f); continue
            pages[t[0]][t[1]] = txt.replace(find, rep_); applied.append(f)
        for f in j.get('inserts') or []:
            t, after, ins = target(f.get('where')), str(f.get('after') or ''), str(f.get('insert') or '').strip()
            if not t or not after or not ins or len(ins) > 1500: failed.append(f); continue
            txt = pages[t[0]][t[1]]
            if txt.count(after) != 1: failed.append(f); continue
            k = txt.index(after) + len(after)
            left, right = txt[:k].rstrip(' '), txt[k:].lstrip(' ')
            pre = '' if ins[:1] in '.,;:!?)]”’' else ' '                          # 插入的字前後補空格（標點不補）
            post = '' if not right or right[:1] in '.,;:!?)]”’' else ' '
            pages[t[0]][t[1]] = left + pre + ins + post + right
            applied.append(f)
        if applied:
            for q in {pno - 1, pno}:                       # 被清空的段落拿掉
                if 0 <= q < len(pages): pages[q] = [x for x in pages[q] if not is_text(x) or x.strip()]
            write_json(os.path.join(d, 'pages.json'), pages)
    return {'ok': bool(j.get('ok')) and not failed, 'applied': applied, 'failed': failed, 't': int(time.time())}


def proof_worker():
    while True:
        bid = PROOFQ.get()
        try: proof_job(bid)
        except Exception as e:
            import traceback; traceback.print_exc()
            update_book(bid, proof={'status': 'error', 'error': str(e)[:300]})


def proof_job(bid):
    from concurrent.futures import ThreadPoolExecutor
    d = os.path.join(LIB, bid)
    with LOCK:
        pages = read_json(os.path.join(d, 'pages.json')) or []
    rec = read_json(os.path.join(d, 'proof.json'), {}) or {}
    todo = [i for i, pg in enumerate(pages) if any(is_text(x) for x in pg) and not (rec.get(str(i + 1)) or {}).get('t')]
    total = sum(1 for pg in pages if any(is_text(x) for x in pg)); done = total - len(todo)
    update_book(bid, proof={'status': 'running', 'done': done, 'total': total})
    imgdir = render_pages(bid, [i + 1 for i in todo])
    stop = [None]
    def one(i):
        if stop[0]: return
        try: r = proof_page(bid, i, imgdir)
        except RuntimeError as e: stop[0] = str(e); return
        if r is None: return
        with LOCK:
            rr = read_json(os.path.join(d, 'proof.json'), {}) or {}; rr[str(i + 1)] = r; write_json(os.path.join(d, 'proof.json'), rr)
            b = find_book(load_index(), bid); pr = dict((b or {}).get('proof') or {})
            pr['done'] = pr.get('done', 0) + 1; pr['rev'] = int(time.time() * 1000)
            update_book(bid, proof=pr)
    with ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(one, todo))
    if stop[0]: update_book(bid, proof={'status': 'error', 'error': stop[0][:300], 'done': done, 'total': total}); return
    rr = read_json(os.path.join(d, 'proof.json'), {}) or {}
    fixed = sum(len(v.get('applied') or []) for v in rr.values() if isinstance(v, dict))
    with LOCK:
        pages = read_json(os.path.join(d, 'pages.json')) or []
    update_book(bid, proof={'status': 'done', 'done': total, 'total': total, 'fixed': fixed, 'rev': int(time.time() * 1000)}, ocrBad=bad_pages(pages))


# ---------- Docling：版面解析（圖片、表格、段落）。裝在 Readiverse 自己的 Python 3.12 環境 ----------
HOME_DIR = os.path.expanduser('~/.readiverse')
VENV = os.path.join(HOME_DIR, 'docling-venv')
VPY = os.path.join(VENV, 'Scripts', 'python.exe') if sys.platform.startswith('win') else os.path.join(VENV, 'bin', 'python')
DOCLING = {'status': 'checking', 'error': ''}      # checking / installing / ready / unavailable / error
DOCLING_READY = threading.Event()


def _find_uv():
    for c in (shutil.which('uv'), os.path.expanduser('~/.local/bin/uv'), os.path.expanduser('~/.cargo/bin/uv'), '/opt/homebrew/bin/uv'):
        if c and os.path.exists(c): return c
    return None


def _find_py310():
    cands = [shutil.which(x) for x in ('python3.13', 'python3.12', 'python3.11', 'python3.10', 'python3', 'python')]
    cands += [os.path.expanduser('~/.local/bin/python3.12'), '/opt/homebrew/bin/python3', '/usr/local/bin/python3']
    for c in cands:
        if not c or not os.path.exists(c): continue
        try:
            v = subprocess.run([c, '-c', 'import sys;print(sys.version_info[:2]>=(3,10))'], capture_output=True, text=True, timeout=20).stdout.strip()
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
        uv = _find_uv()
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


def parse_worker():
    while True:
        bid = PARSEQ.get()
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
    PARSE_PROC[bid] = p; total = b.get('pages') or 0; got = False; lost = {}
    try:
        for line in p.stdout:
            try: j = json.loads(line)
            except ValueError: continue
            if 'total' in j:
                total = j['total']; update_book(bid, scanned=bool(j.get('scanned')), ocr=j.get('ocr') or ''); continue
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
                        if i < len(raw): new = fix_dashes(new, ' '.join(x for x in raw[i] if is_text(x)))
                        if page_text(new).strip() or any(not is_text(x) for x in new) or not page_text(pages[i]).strip():
                            pages[i] = new
                    pages = clean_pages(pages, frozen)
                    write_json(pf, pages)
                update_book(bid, parse={'status': 'running', 'next': j['to'], 'total': total, 'rev': int(time.time() * 1000)},
                            hasText=True if got else b.get('hasText'))
        p.wait()
    finally:
        PARSE_PROC.pop(bid, None)
    if p.returncode not in (0, None) and not got:
        update_book(bid, parse={'status': 'error', 'error': 'Docling 解析失敗（代碼 %s）' % p.returncode}); return
    with LOCK:
        pages = read_json(os.path.join(d, 'pages.json')) or []
        write_json(os.path.join(d, 'lost.json'), lost)
        b = update_book(bid, parse={'status': 'done', 'next': total, 'total': total, 'rev': int(time.time() * 1000)},
                        hasText=sum(len(page_text(pg)) for pg in pages) > 40)
    if b and claude_path() and (read_json(CONFIG, {}) or {}).get('proof', True):
        with LOCK: write_json(os.path.join(d, 'proof.json'), {})       # 重新解析過：整本重新校對
        update_book(bid, proof={'status': 'queued', 'done': 0, 'total': total})  # 記下來：App 關掉再開也會接著校對
        PROOFQ.put(bid)                                                 # AI 逐頁對照原書校對
    elif b and b.get('scanned'): QQ.put(bid)                            # 沒有 Claude：至少檢查亂碼頁


def start_parse(bid, restart=False):
    b = find_book(load_index(), bid)
    if not b: return None
    if DOCLING['status'] in ('unavailable', 'error'):
        return update_book(bid, parse={'status': 'skipped', 'error': DOCLING.get('error') or 'Docling 不能用'})
    nxt = 0 if restart else int((b.get('parse') or {}).get('next') or 0)
    b = update_book(bid, parse={'status': 'queued', 'next': nxt, 'total': b.get('pages') or 0})
    PARSEQ.put(bid); return b


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
                if self.body_json().get('restart'): write_json(os.path.join(d, 'proof.json'), {})
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


def main():
    if LIB == LOCAL_LIB and not os.path.exists(LOCAL_LIB) and os.path.isfile(os.path.join(OLD_LIB, '_index.json')):
        try: shutil.move(OLD_LIB, LOCAL_LIB); print('書庫搬到 %s' % LOCAL_LIB)
        except OSError as e: print('書庫搬家失敗：%s' % e)
    os.makedirs(LIB, exist_ok=True)
    threading.Thread(target=ai_worker, daemon=True).start()
    rv_dict.init(lambda: LIB, ai_json)
    threading.Thread(target=parse_worker, daemon=True).start()
    threading.Thread(target=quality_worker, daemon=True).start()
    threading.Thread(target=proof_worker, daemon=True).start()
    threading.Thread(target=docling_setup, daemon=True).start()
    with LOCK:                                   # 上次沒做完的：接著做
        for b in load_index()['books']:
            if (b.get('ai') or {}).get('status') in ('running', 'queued'): AIQ.put(b['id'])
            if (b.get('parse') or {}).get('status') in ('running', 'queued'): PARSEQ.put(b['id'])
            elif (b.get('proof') or {}).get('status') in ('running', 'queued'): PROOFQ.put(b['id'])
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
    url = 'http://localhost:%d' % PORT
    print('Readiverse 已啟動：%s\n書庫：%s\n要關掉請按 Ctrl+C' % (url, LIB))
    if '--no-browser' not in sys.argv: threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: print('\n已關閉')


if __name__ == '__main__':
    main()
