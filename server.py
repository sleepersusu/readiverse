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

ROOT = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(ROOT, 'static')
CONFIG = os.path.expanduser('~/.readiverse.json')
LOCAL_LIB = os.path.join(ROOT, '書庫')
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


META_KEYS = ('title', 'author', 'category', 'tags', 'status', 'lastPage', 'pages', 'favorite', 'hasText')


def clean_meta(d):
    out = {}
    for k in META_KEYS:
        if k not in d: continue
        v = d[k]
        if k in ('title', 'author', 'category', 'status'): v = str(v or '').strip()[:300]
        elif k == 'tags': v = [str(t).strip()[:40] for t in (v or []) if str(t).strip()][:20]
        elif k in ('lastPage', 'pages'): v = max(0, int(v or 0))
        else: v = bool(v)
        out[k] = v
    return out


# ---------- AI：用電腦上已登入的 Claude Code，不能用就換 Codex（ChatGPT）----------
AI_BAD = {}       # 剛剛失敗的工具：10 分鐘內先跳過，省時間


def ai_cmds(prompt):
    out = []
    claude = shutil.which('claude') or os.path.expanduser('~/.local/bin/claude')
    if os.path.exists(claude):
        out.append(('Claude Code', [claude, '-p', '--model', os.environ.get('READIVERSE_MODEL', 'claude-haiku-5-5'), prompt]))
    codex = shutil.which('codex')
    if codex: out.append(('Codex', [codex, 'exec', '--skip-git-repo-check', prompt]))
    good = [c for c in out if time.time() - AI_BAD.get(c[0], 0) > 600]
    return good + [c for c in out if c not in good]


def ai_json(prompt, want=list, timeout=300):
    """問 AI，回傳 JSON（list 或 dict）。全部工具都失敗就丟 RuntimeError"""
    cmds = ai_cmds(prompt)
    if not cmds: raise RuntimeError('找不到 AI 工具：請先安裝並登入 Claude Code（終端機輸入 claude）或 Codex')
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


# ---------- 匯入後 AI 整理：書名／作者／分類，每一頁重新分段、拿掉頁首頁尾 ----------
AIQ = queue.Queue()
BATCH_CHARS = 9000


def ai_meta(bid, pages, ix):
    b = find_book(ix, bid)
    sample = '\n\n'.join(p for pg in pages[:4] for p in pg)[:5000]
    prompt = ('Below is the beginning of a PDF the user imported (file name: %r). Identify the book/document. '
              'Reply with ONLY a JSON object: {"title": "...", "author": "... or empty", "category": "...", "tags": ["..", ".."]}. '
              'category must be exactly one of %s, or "" if none fits. tags: up to 3 short topic tags in Traditional Chinese.\n\n%s'
              % (b.get('file') or b['title'], json.dumps(ix['categories'], ensure_ascii=False), sample))
    return ai_json(prompt, dict, 180)


def ai_clean_pages(chunk):
    """chunk = {頁碼: [段落…]} → {頁碼: [整理後的段落…]}"""
    prompt = ('These are paragraphs extracted from PDF pages (JSON: page number -> list of paragraphs). The extraction may have '
              'broken paragraphs in the wrong places, hyphenated line-break words, running headers/footers, page numbers, URLs and watermarks. '
              'For each page, return the cleaned list of paragraphs: merge pieces that belong to one paragraph, split ones that '
              'were wrongly joined, rejoin hyphenated words, keep headings and question lines as their own paragraphs, remove running '
              'headers/footers/page numbers/URLs/watermarks. Do NOT translate, summarize or reword anything; keep every other word exactly. '
              'Reply with ONLY a JSON object with the same page keys.\n\n%s' % json.dumps(chunk, ensure_ascii=False))
    return ai_json(prompt, dict, 420)


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
    raw = read_json(os.path.join(d, 'pages_raw.json')) or read_json(os.path.join(d, 'pages.json')) or []
    if not os.path.exists(os.path.join(d, 'pages_raw.json')): write_json(os.path.join(d, 'pages_raw.json'), raw)
    st = dict(b.get('ai') or {}); nxt = int(st.get('next') or 0); total = len(raw)
    update_book(bid, ai={'status': 'running', 'next': nxt, 'total': total, 'step': 'meta' if not st.get('metaDone') else 'pages', 'metaDone': st.get('metaDone')})
    if not st.get('metaDone'):
        try:
            m = ai_meta(bid, raw, ix); up = {}
            with LOCK:
                ix = load_index(); b = find_book(ix, bid)
                if b.get('title') == b.get('autoTitle') and str(m.get('title') or '').strip(): up['title'] = str(m['title']).strip()[:300]
                if not b.get('author') and str(m.get('author') or '').strip(): up['author'] = str(m['author']).strip()[:200]
                if not b.get('category') and m.get('category') in ix['categories']: up['category'] = m['category']
                if not b.get('tags') and isinstance(m.get('tags'), list): up['tags'] = [str(t)[:40] for t in m['tags'][:3]]
                b.update(up); b['ai'] = dict(b.get('ai') or {}, metaDone=True, step='pages'); save_index(ix)
        except RuntimeError as e:
            update_book(bid, ai={'status': 'error', 'error': str(e)[:300], 'next': 0, 'total': total}); return
    errs = 0
    while nxt < total:
        chunk, size, i = {}, 0, nxt
        while i < total and (not chunk or size + len(''.join(raw[i])) < BATCH_CHARS):
            if raw[i]: chunk[str(i + 1)] = raw[i]
            size += len(''.join(raw[i])); i += 1
        if chunk:
            try: res = ai_clean_pages(chunk)
            except RuntimeError as e:
                errs += 1
                if errs >= 2: update_book(bid, ai=dict((find_book(load_index(), bid) or {}).get('ai') or {}, status='error', error=str(e)[:300], next=nxt)); return
                continue
            with LOCK:
                pf = os.path.join(d, 'pages.json'); pages = read_json(pf) or raw
                state = read_json(os.path.join(d, 'state.json'), {}) or {}
                used = {int(k.split(':')[0]) for k, v in (state.get('marks') or {}).items() if v} | {int(k) for k in (state.get('zh') or {})} \
                    | {int(k.split(':')[0]) for k, v in (state.get('paraNotes') or {}).items() if v}
                for k, v in res.items():
                    try: p = int(k) - 1
                    except ValueError: continue
                    if p in used or not (0 <= p < len(pages)) or not isinstance(v, list): continue     # 已經畫線／翻譯過的頁不動（位置會跑掉）
                    v = [re.sub(r'\s+', ' ', str(x)).strip() for x in v if str(x).strip()]
                    before = len(re.sub(r'\s', '', ''.join(raw[p])))
                    if before and len(re.sub(r'\s', '', ''.join(v))) < before * 0.6: continue      # 少太多字：不信任，留原本的
                    pages[p] = v
                write_json(pf, pages)
        nxt = i
        update_book(bid, ai={'status': 'running', 'step': 'pages', 'next': nxt, 'total': total, 'metaDone': True, 'rev': int(time.time())})
    update_book(bid, ai={'status': 'done', 'next': total, 'total': total, 'metaDone': True, 'rev': int(time.time())})


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
                upd = clean_meta(self.body_json())
                with LOCK:
                    ix = load_index(); b = find_book(ix, bid)
                    if 'lastPage' in upd: b['opened'] = int(time.time() * 1000)
                    if upd.get('lastPage') and b.get('status') == 'unread' and 'status' not in upd: upd['status'] = 'reading'
                    b.update(upd); save_index(ix)
                return self.send(200, b)
            if sub == '' and m == 'DELETE':
                with LOCK:
                    ix = load_index(); ix['books'] = [x for x in ix['books'] if x['id'] != bid]; save_index(ix)
                    shutil.rmtree(d, ignore_errors=True)
                return self.send(200, {'ok': True})
            if sub == 'pages' and m == 'PUT':
                pages = self.body_json().get('pages')
                if not isinstance(pages, list): return self.err(400, 'pages 格式不對')
                pages = [[str(x) for x in pg] if isinstance(pg, list) else [] for pg in pages]
                write_json(os.path.join(d, 'pages.json'), pages); write_json(os.path.join(d, 'pages_raw.json'), pages)
                b = update_book(bid, pages=len(pages), hasText=sum(len(''.join(pg)) for pg in pages) > 40)
                return self.send(200, b)
            if sub == 'ai' and m == 'POST':          # 開始（或重新開始）AI 整理
                if (b.get('ai') or {}).get('status') == 'running': return self.send(200, b)
                if not b.get('hasText'): return self.err(400, '這本 PDF 抽不出文字，沒辦法整理')
                again = bool(self.body_json().get('restart'))
                b = update_book(bid, ai={'status': 'queued', 'next': 0 if again else int((b.get('ai') or {}).get('next') or 0),
                                         'total': b.get('pages') or 0, 'metaDone': False if again else (b.get('ai') or {}).get('metaDone')})
                AIQ.put(bid)
                return self.send(200, b)
            if sub == 'cover' and m == 'PUT':
                data = self.body_bytes()
                if data[:2] != b'\xff\xd8': return self.err(400, '封面要是 JPEG')
                with open(os.path.join(d, 'cover.jpg'), 'wb') as f: f.write(data)
                return self.send(200, update_book(bid, cover=True, coverV=int(time.time() * 1000)))
            if sub == 'cover' and m == 'DELETE':           # 不用圖片，改用設計好的封面（書名＋作者）
                try: os.remove(os.path.join(d, 'cover.jpg'))
                except OSError: pass
                return self.send(200, update_book(bid, cover=False, coverV=int(time.time() * 1000)))
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
    os.makedirs(LIB, exist_ok=True)
    threading.Thread(target=ai_worker, daemon=True).start()
    with LOCK:                                   # 上次沒做完的 AI 整理：接著做
        for b in load_index()['books']:
            if (b.get('ai') or {}).get('status') in ('running', 'queued'): AIQ.put(b['id'])
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
    url = 'http://localhost:%d' % PORT
    print('Readiverse 已啟動：%s\n書庫：%s\n要關掉請按 Ctrl+C' % (url, LIB))
    if '--no-browser' not in sys.argv: threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: print('\n已關閉')


if __name__ == '__main__':
    main()
