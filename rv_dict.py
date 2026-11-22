"""Readiverse：字典、單字收藏、句子收藏（做法跟雅思練習室一樣，資料各自分開，可以從雅思練習室匯入）

書庫/
  _單字收藏.json   {"vocab": [{w, k, ctx, title, book, page, t, src, forgot, forgotT, note}]}   新的在前面
  _句子收藏.json   [{id, text, ctx, title, book, page, t, status, error, key, zh, pattern, note, examples, more}]
  _字典快取.json   {單字: {t, src: cambridge|local|none, ex: [[英文, 中文]], hw, tried, exBy}}
"""
import json, os, re, threading, time, queue, glob, shutil, subprocess, urllib.parse
from html.parser import HTMLParser

LOCK = threading.RLock()
_get_lib = None
_ai_json = None


def init(get_lib, ai_json):
    global _get_lib, _ai_json
    _get_lib, _ai_json = get_lib, ai_json
    threading.Thread(target=_phrase_worker, daemon=True).start()
    with LOCK:                                     # 上次沒做完的句子分析：接著做
        for x in phrases():
            if x.get('status') in ('pending', 'running'): PHQ.put(x['id'])


def _read(p, d):
    try:
        with open(p, encoding='utf-8') as f: return json.load(f)
    except Exception: return d


def _write(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.%d.tmp' % threading.get_ident()
    with open(tmp, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, p)


def VOCF(): return os.path.join(_get_lib(), '_單字收藏.json')
def PHRF(): return os.path.join(_get_lib(), '_句子收藏.json')
def CACHEF(): return os.path.join(_get_lib(), '_字典快取.json')


def wkey(w): return re.sub(r"[^a-z0-9' -]", '', str(w).lower().replace('’', "'")).strip()


# ---------- 本機字典（ECDICT，跟雅思練習室同一份 字典.json；沒有也可以，只是少了離線查詢）----------
HERE = os.path.dirname(os.path.abspath(__file__))
DICT_CANDS = [os.path.join(HERE, '字典.json'), os.path.expanduser('~/.readiverse/字典.json'),
              os.path.expanduser('~/Downloads/ielts-practice-room/字典.json'), os.path.expanduser('~/Downloads/雅思聽寫練習室/字典.json')]
_dict = None


def local_dict():
    global _dict
    if _dict is None:
        p = next((c for c in DICT_CANDS if os.path.exists(c)), None)
        _dict = _read(p, {'d': {}, 'inf': {}}) if p else {'d': {}, 'inf': {}}
    return _dict


def candidates(k):
    INF = local_dict().get('inf', {})
    c = [k, INF.get(k, '')]
    if k.endswith('ies'): c.append(k[:-3] + 'y')
    if k.endswith('es'): c.append(k[:-2])
    if k.endswith('s') and not k.endswith('ss'): c.append(k[:-1])
    if k.endswith('ied'): c.append(k[:-3] + 'y')
    if k.endswith('ed'): c += [k[:-2], k[:-1]]
    if k.endswith('ing'): c += [k[:-3], k[:-3] + 'e']
    if k.endswith("'s"): c.append(k[:-2])
    c += [x.replace('ise', 'ize') for x in list(c) if 'ise' in x]
    out = []
    for x in c:
        if x and x not in out: out.append(x)
    return out


def local_lookup(k):
    D = local_dict().get('d', {})
    for c in candidates(k):
        if c in D: return D[c] if c == k else '（%s）%s' % (c, D[c])
    return ''


# ---------- Cambridge 英漢（繁體）----------
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36'
POS = {'noun': 'n.', 'verb': 'v.', 'adjective': 'adj.', 'adverb': 'adv.', 'preposition': 'prep.', 'conjunction': 'conj.',
       'pronoun': 'pron.', 'determiner': 'det.', 'exclamation': 'excl.', 'phrasal verb': 'phr v.', 'idiom': 'idiom',
       'modal verb': 'modal v.', 'auxiliary verb': 'aux v.', 'number': 'num.', 'predeterminer': 'predet.', 'suffix': 'suffix', 'prefix': 'prefix'}


def fetch(url):
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept-Language': 'en-GB,en;q=0.9'})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.geturl(), r.read().decode('utf-8', 'ignore')
    except Exception:
        try:
            out = subprocess.run(['curl', '-sL', '--max-time', '8', '-A', UA, '-w', '\n__URL__%{url_effective}', url],
                                 capture_output=True, timeout=12).stdout.decode('utf-8', 'ignore')
            body, _, final = out.rpartition('\n__URL__')
            return final.strip(), body
        except Exception:
            return '', ''


class CamParser(HTMLParser):
    """抓 Cambridge 英漢（繁體）頁面：詞性、每個義項的中文、例句（英文＋中文）"""
    def __init__(self):
        super().__init__(); self.stack = []; self.pos = ''; self.cap = None; self.buf = ''; self.items = []
        self.headword = ''; self.hw_cap = False; self.examples = []; self.reg = ''
    def _in(self, cls): return any(cls in c for t, c in self.stack)
    def handle_starttag(self, tag, attrs):
        a = dict(attrs); cls = a.get('class', '') or ''; cs = cls.split()
        if tag not in ('br', 'img', 'input', 'meta', 'link', 'source', 'hr'): self.stack.append((tag, cls))
        if tag == 'div' and 'dsense' in cs: self.reg = ''
        if tag == 'span' and ('dregion' in cs or 'dusage' in cs) and not self._in('examp'): self.cap = 'reg'; self.buf = ''
        elif tag == 'span' and 'deg' in cs and self._in('examp'): self.cap = 'eg'; self.buf = ''
        elif tag == 'span' and 'dtrans' in cs and a.get('lang') == 'zh-Hant' and self._in('examp'): self.cap = 'egtr'; self.buf = ''
        elif tag == 'span' and 'dpos' in cs: self.cap = 'pos'; self.buf = ''
        elif 'dtrans' in cs and a.get('lang') == 'zh-Hant' and not self._in('examp') and not self._in('dexamp'): self.cap = 'tr'; self.buf = ''
        elif 'hw' in cs and 'dhw' in cs and not self.headword: self.hw_cap = True; self.buf_hw = ''
    def handle_endtag(self, tag):
        if self.cap and self.stack and self.stack[-1][0] == tag:
            cls = self.stack[-1][1].split()
            if self.cap == 'pos' and 'dpos' in cls: self.pos = self.buf.strip(); self.cap = None
            elif self.cap == 'eg' and 'deg' in cls:
                t = re.sub(r'\s+', ' ', self.buf).strip(); self.cap = None
                if t and len(self.examples) < 6: self.examples.append([t, ''])
            elif self.cap == 'egtr' and 'dtrans' in cls:
                t = re.sub(r'\s+', '', self.buf).strip(); self.cap = None
                if t and self.examples and not self.examples[-1][1]: self.examples[-1][1] = t
            elif self.cap == 'reg' and ('dregion' in cls or 'dusage' in cls):
                r = self.buf.strip(); self.cap = None
                lab = '英式' if r in ('UK', 'mainly UK') else '美式' if r in ('US', 'mainly US') else ''
                if lab: self.reg = lab
            elif self.cap == 'tr' and 'dtrans' in cls:
                t = re.sub(r'\s+', '', self.buf.strip())
                if t and self.reg: t = '（%s）%s' % (self.reg, t)
                if t: self.items.append((self.pos, t))
                self.cap = None
        if self.hw_cap and self.stack and self.stack[-1][0] == tag:
            self.headword = self.buf_hw.strip(); self.hw_cap = False
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag: del self.stack[i:]; break
    def handle_data(self, d):
        if self.cap: self.buf += d
        if self.hw_cap: self.buf_hw += d


CAM_BLOCKED = [0]      # Cambridge 擋自動連線（Cloudflare）時：一天內不再試


def cambridge(word):
    if time.time() < CAM_BLOCKED[0]: return None
    final, html = fetch('https://dictionary.cambridge.org/dictionary/english-chinese-traditional/' + urllib.parse.quote(word))
    f = final or ''
    if not html: return None                       # 連不上
    if '<title>Just a moment' in html[:600] or 'challenge-platform' in html[:3000]:
        CAM_BLOCKED[0] = time.time() + 86400; return None
    ok_url = 'english-chinese-traditional' in f or '英語-漢語-繁體' in urllib.parse.unquote(f)
    last = urllib.parse.unquote(f.split('?')[0].rstrip('/').split('/')[-1])
    if not ok_url or 'spellcheck' in f or 'search' in f.lower() or last in ('english-chinese-traditional', '英語-漢語-繁體'):
        return ('', '', [])
    p = CamParser()
    try: p.feed(html)
    except Exception: pass
    groups = {}
    for pos, t in p.items:
        ab = POS.get(pos.lower(), pos.lower() + '.' if pos else '')
        lst = groups.setdefault(ab, [])
        for piece in re.split(r'[；;]', t):
            piece = piece.strip(' ，,')
            if piece and piece not in lst and len(lst) < 4: lst.append(piece)
        if len(groups) > 3: break
    text = '　'.join(('%s %s' % (k, '；'.join(v))).strip() for k, v in groups.items() if v)
    return (text, p.headword, p.examples[:3])


def freedict(w):
    """Free Dictionary API（dictionaryapi.dev，免費、不用金鑰）：音標、發音音檔、英英解釋、例句"""
    import urllib.request
    try:
        req = urllib.request.Request('https://api.dictionaryapi.dev/api/v2/entries/en/' + urllib.parse.quote(w), headers={'User-Agent': 'Readiverse'})
        with urllib.request.urlopen(req, timeout=8) as r: j = json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return {} if e.code == 404 else None
    except Exception:
        return None
    if not isinstance(j, list) or not j: return {}
    ph = next((x.get('phonetic') for x in j if x.get('phonetic')), '') or next((p.get('text') for x in j for p in x.get('phonetics', []) if p.get('text')), '')
    audio = ''
    for x in j:
        for p in x.get('phonetics', []):
            a = p.get('audio') or ''
            if a and (not audio or '-uk' in a): audio = a
    defs = []
    for x in j:
        for m in x.get('meanings', []):
            for d in m.get('definitions', [])[:2]:
                if len(defs) < 5: defs.append({'pos': m.get('partOfSpeech', ''), 'def': d.get('definition', ''), 'ex': d.get('example', '')})
    return {'phonetic': ph, 'audio': audio, 'defs': defs}


def lookup(w):
    """查一個字（或片語）：快取 → Cambridge → 本機字典"""
    k = wkey(w)
    if not k: return {'t': '', 'src': 'none'}
    with LOCK:
        cache = _read(CACHEF(), {})
        c = cache.get(k)
    if c and 'en' not in c and len(k.split()) <= 3:          # 還沒查過英英：補上
        en = freedict(k)
        if en is not None:
            with LOCK:
                cache = _read(CACHEF(), {}); c = cache.get(k) or c; c['en'] = en; cache[k] = c; _write(CACHEF(), cache)
    if c and (c.get('src') == 'cambridge' or time.time() - c.get('tried', 0) < 7 * 86400): return c
    res, offline = None, False
    for cand in candidates(k)[:3]:
        r = cambridge(cand)
        if r is None: offline = True; break
        if r[0]:
            res = {'t': r[0] if cand == k else '（%s）%s' % (cand, r[0]), 'src': 'cambridge', 'ex': r[2], 'hw': r[1]}; break
    if not res:
        t = local_lookup(k)
        res = {'t': t, 'src': 'local' if t else 'none', 'tried': time.time()}
        if offline: res['offline'] = True
    if len(k.split()) <= 3:
        en = freedict(k)
        if en is not None: res['en'] = en
    with LOCK:
        cache = _read(CACHEF(), {}); old = cache.get(k) or {}
        if old.get('ex') and not res.get('ex'): res['ex'] = old['ex']; res['exBy'] = old.get('exBy')
        if 'en' not in res and 'en' in old: res['en'] = old['en']
        cache[k] = res; _write(CACHEF(), cache)
    return res


def explain(w, ctx=''):
    """AI：這個字（片語）在這個句子裡是什麼意思"""
    j = _ai_json('An English learner is reading a book and does not understand "%s"%s. Explain it for them. Reply with ONLY JSON: '
                 '{"pos": "part of speech (n./v./adj./adv./phr v./idiom...)", "zh": "its meaning IN THIS SENTENCE in natural Traditional Chinese (台灣用語), short", '
                 '"base": "dictionary form if different, else empty", "note": "1 short sentence in Traditional Chinese if there is something worth knowing (nuance, register, common collocation), else empty", '
                 '"ex": [["another natural example sentence using it in the same meaning", "中文翻譯"]]}'
                 % (w, (' in this sentence: "%s"' % str(ctx)[:500]) if ctx else ''), dict, 120)
    return {k: j.get(k) for k in ('pos', 'zh', 'base', 'note', 'ex')}


def ai_examples(w):
    """Cambridge 沒有例句時：請 AI 寫 2 句（附中文）"""
    k = wkey(w)
    with LOCK:
        c = _read(CACHEF(), {}).get(k) or {}
    if c.get('ex'): return c['ex']
    j = _ai_json('Write 2 natural example sentences in British English using the word or phrase "%s" in its most common meaning, '
                 'each with a natural Traditional Chinese (台灣用語) translation. Reply with ONLY JSON: {"ex": [["English sentence", "中文翻譯"], ["...", "..."]]}' % w, dict, 120)
    ex = [[str(a), str(b)] for a, b in (x for x in (j.get('ex') or []) if isinstance(x, (list, tuple)) and len(x) == 2)][:3]
    if ex:
        with LOCK:
            cache = _read(CACHEF(), {}); e = cache.get(k) or {}; e['ex'] = ex; e['exBy'] = 'ai'; cache[k] = e; _write(CACHEF(), cache)
    return ex


# ---------- 單字收藏 ----------
def vocab():
    with LOCK:
        j = _read(VOCF(), {})
        return j.get('vocab') or [] if isinstance(j, dict) else []


def _save_vocab(L):
    _write(VOCF(), {'vocab': L})


def vocab_marks():
    """文章裡畫底線用：每個收藏的字＋快取裡的中文（不連網路）"""
    with LOCK:
        cache = _read(CACHEF(), {})
    return [dict(v, m=(cache.get(v['k']) or {}).get('t', '')) for v in vocab()]


def vocab_add(w, ctx='', title='', book='', page=None, src='reading'):
    w = re.sub(r'\s+', ' ', str(w)).strip(" .,;:!?\"“”‘’()")[:80]; k = wkey(w)
    if not k: raise ValueError('不是英文單字')
    with LOCK:
        L = vocab(); old = next((v for v in L if v['k'] == k), None)
        if old: dup = True
        else:
            dup = False
            L.insert(0, {'w': w, 'k': k, 'ctx': str(ctx)[:600], 'title': str(title)[:200], 'book': book, 'page': page, 't': int(time.time() * 1000), 'src': src})
            _save_vocab(L)
    m = lookup(w)
    return {'dup': dup, 'k': k, 'meaning': m}


def vocab_update(k, **kw):
    with LOCK:
        L = vocab(); v = next((x for x in L if x['k'] == k), None)
        if not v: return None
        v.update(kw); _save_vocab(L); return v


def vocab_del(k):
    with LOCK:
        L = [v for v in vocab() if v['k'] != k]; _save_vocab(L)


# ---------- 句子收藏：AI 在背景抽出值得學的片語、句型 ----------
PHQ = queue.Queue()


def phrases():
    with LOCK:
        j = _read(PHRF(), [])
        return j if isinstance(j, list) else []


def _phr_update(pid, **kw):
    with LOCK:
        L = phrases(); x = next((p for p in L if p.get('id') == pid), None)
        if x: x.update(kw); _write(PHRF(), L)
        return x


def phrase_add(text, ctx='', title='', book='', page=None, src='reading'):
    text = re.sub(r'\s+', ' ', str(text)).strip()[:300]
    if len(text.split()) < 2: raise ValueError('句子至少要兩個字')
    with LOCK:
        L = phrases(); low = text.lower()
        d = next((p for p in L if (p.get('text') or '').lower() == low), None)
        if d: return {'dup': True, 'id': d['id']}
        pid = 'p%d' % int(time.time() * 1000)
        L.insert(0, {'id': pid, 'text': text, 'ctx': str(ctx)[:600], 'title': str(title)[:200], 'book': book, 'page': page,
                     'src': src, 't': int(time.time() * 1000), 'status': 'pending'})
        _write(PHRF(), L)
    PHQ.put(pid)
    return {'dup': False, 'id': pid}


def phrase_del(pid):
    with LOCK:
        L = [p for p in phrases() if p.get('id') != pid]; _write(PHRF(), L)


def phrase_retry(pid):
    if _phr_update(pid, status='pending', error=''): PHQ.put(pid)


def _phrase_worker():
    while True:
        pid = PHQ.get()
        try: phrase_classify(pid)
        except Exception as e: _phr_update(pid, status='error', error=str(e)[:300])


def phrase_classify(pid):
    x = next((p for p in phrases() if p.get('id') == pid), None)
    if not x: return
    _phr_update(pid, status='running', error='')
    others = [p for p in phrases() if p.get('id') != pid and p.get('status') == 'done'][:150]
    known = '\n'.join('%s | %s | %s' % (p['id'], p.get('key') or p.get('text', ''), p.get('pattern', '')) for p in others)
    prompt = ('An English learner reading a book selected this text and saved it, because it might be useful in their own English writing and speaking:\n"'
              + x['text'] + '"\n' + ('It appeared in this sentence: "' + x['ctx'][:600] + '"\n' if x.get('ctx') else '') +
              'Learners often select MORE than the useful part. Find the expressions worth learning in it: each one a SHORT reusable collocation, '
              'idiom, phrasal verb, grammar structure or linking phrase (usually 2-6 words, e.g. "take something for granted", "largely because of", '
              '"it never occurred to me that"), copied EXACTLY from the selection as a continuous substring. Leave out topic words, names, numbers and dates '
              'that are not part of the expression. Usually there is ONE; give a separate item only if the selection really contains more than one useful one (max 3).\n'
              + ('The learner has already saved these expressions (id | key | pattern):\n' + known + '\n'
                 'For each item, if it is the SAME expression in another form or tense, or the same structure with different words filled in, set "same_as" '
                 'to that id. A different meaning is NOT the same. Otherwise null.\n' if known else '') +
              'Reply with ONLY one JSON object:\n'
              '{"items": [{"key": "the expression, copied exactly from the selection",\n'
              '  "same_as": null,\n'
              '  "zh": "its meaning in natural Traditional Chinese (台灣用語)",\n'
              '  "pattern": "the reusable pattern, e.g. \'take + sth + for granted\' or \'It never occurred to sb that + clause\'",\n'
              '  "note": "1-2 sentences in Traditional Chinese: when and how to use it, register (formal/informal), common mistakes",\n'
              '  "examples": [{"en": "a natural sentence using it, with the expression wrapped in **double asterisks**", "zh": "中文翻譯（中文裡對應的說法也用 **  ** 標出來）"}]}]}\n'
              'Give 2 examples per item. For an item with "same_as", you may leave zh, pattern, note and examples empty.')
    j = _ai_json(prompt, dict, 240)
    items = [it for it in (j.get('items') or ([j] if j.get('key') else [])) if isinstance(it, dict)][:3]
    if not items: raise RuntimeError('AI 沒有回傳結果，可以再試一次')
    ids = {p['id'] for p in others}
    with LOCK:
        L = phrases(); me = next((p for p in L if p.get('id') == pid), None)
        if not me: return
        extra = []
        for n, it in enumerate(items):
            key = str(it.get('key') or '').strip()
            if key and key.lower() not in me['text'].lower(): key = ''
            fields = {'key': key, 'zh': str(it.get('zh') or ''), 'pattern': str(it.get('pattern') or ''), 'note': str(it.get('note') or ''),
                      'examples': [e for e in (it.get('examples') or []) if isinstance(e, dict)][:4], 'status': 'done', 'error': ''}
            same = it.get('same_as')
            if same in ids:                         # 已經收藏過同一個說法：記在那一筆底下
                tgt = next(p for p in L if p.get('id') == same)
                tgt.setdefault('more', []).append({'text': me['text'], 'ctx': me.get('ctx', ''), 'title': me.get('title', ''), 't': me['t']})
                if n == 0: me['_merged'] = same
                continue
            if n == 0 or me.get('_merged'): me.update(fields); me.pop('_merged', None)
            else: extra.append(dict(me, id='%s-%d' % (pid, n), **fields))
        if me.get('_merged'): L = [p for p in L if p.get('id') != pid]
        else:
            i = L.index(me); L[i + 1:i + 1] = extra
        _write(PHRF(), L)


# ---------- 從雅思練習室匯入 ----------
def ielts_lib():
    home = os.path.expanduser('~')
    cands = [os.path.join(home, 'Downloads', '雅思聽寫練習室', '題庫'), os.path.join(home, 'Downloads', 'ielts-practice-room', '題庫'),
             os.path.join(home, '雅思練習室', '題庫'), os.path.join(home, 'Documents', '雅思練習室', '題庫')]
    cands += glob.glob(os.path.join(home, 'Library', 'CloudStorage', '*', '*', '雅思練習室備份'))
    for c in cands:
        if os.path.exists(os.path.join(c, '_練習紀錄.json')) or os.path.exists(os.path.join(c, '_句子收藏.json')): return c
    return None


def ielts_info():
    L = ielts_lib()
    if not L: return {'found': False}
    v = (_read(os.path.join(L, '_練習紀錄.json'), {}) or {}).get('vocab') or []
    p = _read(os.path.join(L, '_句子收藏.json'), []) or []
    return {'found': True, 'path': L, 'vocab': len(v), 'phrases': len(p)}


def ielts_import():
    L = ielts_lib()
    if not L: raise ValueError('找不到可以匯入的收藏')
    src_v = (_read(os.path.join(L, '_練習紀錄.json'), {}) or {}).get('vocab') or []
    src_p = _read(os.path.join(L, '_句子收藏.json'), []) or []
    src_c = _read(os.path.join(L, '_字典快取.json'), {}) or {}
    nv = np_ = 0
    with LOCK:
        V = vocab(); have = {v['k'] for v in V}
        for v in src_v:
            k = v.get('k') or wkey(v.get('w', ''))
            if not k or k in have: continue
            have.add(k); nv += 1
            V.append({'w': v.get('w') or k, 'k': k, 'ctx': v.get('ctx', ''), 'title': v.get('title', ''), 'book': '', 'page': None,
                      't': v.get('t') or 0, 'src': 'ielts', 'forgot': v.get('forgot', 0), 'forgotT': v.get('forgotT'), 'note': v.get('note', '')})
        V.sort(key=lambda v: v.get('t') or 0, reverse=True); _save_vocab(V)
        P = phrases(); seen = {(p.get('text') or '').lower() for p in P}
        for p in src_p:
            t = (p.get('text') or '').strip()
            if not t or t.lower() in seen: continue
            seen.add(t.lower()); np_ += 1
            q = {k: p[k] for k in ('id', 'text', 'ctx', 'title', 't', 'status', 'error', 'key', 'task', 'zh', 'pattern', 'note', 'examples', 'more', 'noMark') if k in p}
            q['src'] = 'ielts'; q['book'] = ''
            if q.get('status') in ('pending', 'running'): q['status'] = 'error'; q['error'] = '匯入時還沒分析完，可以按重新分析'
            P.append(q)
        P.sort(key=lambda p: p.get('t') or 0, reverse=True); _write(PHRF(), P)
        cache = _read(CACHEF(), {}); nc = 0
        for k, c in src_c.items():
            if k not in cache and isinstance(c, dict) and c.get('t'): cache[k] = c; nc += 1
        _write(CACHEF(), cache)
    dst = os.path.expanduser('~/.readiverse/字典.json')            # 本機字典也複製一份（離線也能查）
    if not os.path.exists(dst):
        for c in DICT_CANDS[2:]:
            if os.path.exists(c):
                try: os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.copy2(c, dst)
                except OSError: pass
                break
    return {'vocab': nv, 'phrases': np_, 'meanings': nc}
