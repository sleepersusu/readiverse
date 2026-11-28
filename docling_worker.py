"""Readiverse：用 Docling 解析一本 PDF（在 Readiverse 自己的 Python 3.12 環境裡執行，由 server.py 叫起來）

用法：python docling_worker.py <book.pdf> <圖片資料夾> [每次幾頁=20] [從第幾頁開始=1]
每處理完一批頁，就在 stdout 印一行 JSON：
  {"total": 總頁數}
  {"from": 1, "to": 20, "pages": {"1": [段落或圖表…], …}}
段落是字串；圖片是 {"t":"fig","src":"p12_0.png","cap":"Figure 6.2 …"}；
表格是 {"t":"table","src":"p90_t0.png","rows":[[格, 格…], …],"cap":"Table 6.1 …"}
"""
import json, re, os, sys, statistics


_WORDS = None
def _word(w):
    """英文單字在不在字典裡（App 附的 ECDICT）。字典讀不到就回 None"""
    global _WORDS
    if _WORDS is None:
        _WORDS = {}
        here = os.path.dirname(os.path.abspath(__file__))
        for f in (os.path.join(here, 'assets', 'dict.json.gz'), os.path.expanduser('~/.readiverse/字典.json')):
            try:
                if f.endswith('.gz'):
                    import gzip
                    with gzip.open(f, 'rt', encoding='utf-8') as fh: _WORDS = json.load(fh).get('d') or {}
                else:
                    with open(f, encoding='utf-8') as fh: _WORDS = json.load(fh).get('d') or {}
                if _WORDS: break
            except Exception: pass
    if not _WORDS: return None
    w = w.lower()
    if w in _WORDS: return True
    for suf, r in (('ies', 'y'), ('es', ''), ('s', ''), ('ied', 'y'), ('ed', ''), ('ed', 'e'), ('ing', ''), ('ing', 'e'), ('ly', '')):
        if w.endswith(suf) and (w[:-len(suf)] + r) in _WORDS: return True
    return False


def hy_join(a, b):
    """行尾有連字號的兩行接起來：peo- / ple → people；twenty- / four → twenty-four（本來就是複合字，連字號要留著）"""
    m1 = re.search(r'([A-Za-z]+)-$', a); m2 = re.match(r'([A-Za-z]+)', b)
    if m1 and m2:
        x, y = m1.group(1), m2.group(1)
        whole, left, right = _word(x + y), _word(x), _word(y)
        if whole is not None and not whole and left and right: return a + b      # 接起來不是字、兩半都是字：複合字
    return a[:-1] + b

def is_scanned(pd):
    """抽樣幾頁：整頁幾乎都是一張圖 → 掃描書"""
    import pypdfium2.raw as R
    n = len(pd); idx = sorted({min(n - 1, int(n * f)) for f in (0.1, 0.3, 0.5, 0.7, 0.9)}) if n else []
    hit = 0
    for i in idx:
        pg = pd[i]; W, H = pg.get_size(); best = 0
        for obj in pg.get_objects(filter=[R.FPDF_PAGEOBJ_IMAGE], max_depth=2):
            l, b, r, t = obj.get_bounds(); best = max(best, max(0, r - l) * max(0, t - b) / (W * H))
        if best > 0.8: hit += 1
    return bool(idx) and hit >= max(1, len(idx) * 0.6)


def has_ocrmac():
    try:
        import ocrmac  # noqa
        return True
    except Exception: return False


import re as _re
# Apple Vision 常把破折號「—」讀成「-」：一般單字後面接虛詞（football-that、hockey-and）→ 改回「—」；face-to-face 這種一串的不動
DASH = _re.compile(r"(?<![\w-])([A-Za-z]{3,})-(that|and|the|but|or|which|who|whom|not|even|if|so|yet|then|when|while|as|because|though|although|just|perhaps|maybe|in|on|at|for|with|from|to|of|a|an|it|he|she|they|we|i|you|this|these|those|one|all|no|nothing|something|everything|what|where|how|why|my|his|her|their|our|its|was|is|are|were|had|has|have|would|could|should|will|can|did|do|does)\b(?!-)", _re.I)


def vision_page(pg):
    """掃描書的一頁：Apple Vision 辨識每一行（位置、大小），照排版自己分段（Docling 會把 OCR 的行順序弄亂）
    - 頁首（書名、章名）：第一行、短、置中、底下空隙小 → 丟掉
    - 章名：比內文大，或第一行短、置中、底下空隙大 → 留著當標題
    - 頁碼：最後一行只有數字 → 丟掉
    - 新段落：首行縮排、空一行、上一行短又是句尾
    - 章名後第一段：首字放大／花紋被辨識成的亂碼（「ace The…」「me The…」）拿掉"""
    import re
    from ocrmac import ocrmac
    img = pg.render(scale=2.5).to_pil()
    raw = [(t.strip(), x, y, w, h) for t, c, (x, y, w, h) in ocrmac.OCR(img, recognition_level='accurate', language_preference=['en-US']).recognize() if t.strip()]
    if not raw: return []
    # 同一行被切成好幾段（「said.」和「"You and I…」）：垂直位置重疊的併成一行，由左到右接起來
    raw.sort(key=lambda r: -(r[2] + r[4]))
    L = []
    for t, x, y, w, h in raw:
        for k, (t2, x2, y2, w2, h2) in enumerate(L):
            ov = min(y + h, y2 + h2) - max(y, y2)
            if ov > 0.5 * min(h, h2):
                segs = sorted([(x2, t2, w2), (x, t, w)])
                nx, nw = min(x, x2), max(x + w, x2 + w2) - min(x, x2)
                L[k] = (' '.join(sg[1] for sg in segs), nx, min(y, y2), nw, max(y + h, y2 + h2) - min(y, y2)); break
        else: L.append((t, x, y, w, h))
    L.sort(key=lambda r: -(r[2] + r[4]))                        # 由上往下
    hs = sorted(r[4] for r in L); medh = hs[len(hs) // 2]
    xs = sorted(r[1] for r in L); left = xs[max(0, len(xs) // 10)]
    ws = sorted(r[3] for r in L); maxw = ws[min(len(ws) - 1, len(ws) * 9 // 10)]
    while L and re.fullmatch(r'[\divxlcIVXLC]{1,5}', L[-1][0]): L.pop()          # 頁碼
    if not L: return []
    heads = set()
    if len(L) > 1:
        t, x, y, w, h = L[0]; nxt = L[1]
        gap = y - (nxt[2] + nxt[4]); cx = x + w / 2
        short = w < maxw * 0.7 and len(t.split()) <= 10 and not re.search(r'[.!?,;]$', t)
        centered = abs(cx - 0.5) < 0.08
        if short and h > medh * 1.3: heads.add(0)                                           # 章名：字比內文大
        elif short and centered and y + h > 0.8: L = L[1:]                                    # 頁首（書名、章名，字不大、在最上面）
    out, cur, prev, junk = [], None, None, []
    for k, (t, x, y, w, h) in enumerate(L):
        head = k in heads or (h > medh * 1.3 and w < maxw * 0.75 and len(t.split()) <= 10 and not re.search(r'[.!?,;]$', t))
        new = (prev is None or head or prev['head'] or x > left + 0.025
               or (prev['y'] - (y + h)) > medh * 1.2
               or (prev['w'] < maxw * 0.85 and re.search(r'[.!?:"”’]$', prev['t'])))
        if new:
            if cur is not None: out.append(cur)
            if prev is not None and prev['head'] and not head:
                m = re.match(r'^([a-z]{1,4})\s+(?=[A-Z])', t)          # 花紋、首字放大被辨識成的亂碼
                if m: junk.append(m.group(1)); t = t[m.end():]
            t = re.sub(r'^([A-Z])\s+(?=\1[a-z])', '', t)            # 「A As my old professor」
            cur = t
        elif re.search(r'[A-Za-z]-$', cur) and re.match(r'[a-z]', t): cur = cur[:-1] + t
        else: cur = cur + ' ' + t
        prev = {'t': t, 'y': y, 'w': w, 'head': head}
    if cur is not None: out.append(cur)
    out = [DASH.sub(lambda m: m.group(1) + '—' + m.group(2), x) for x in out]
    # 小節開頭的花紋被認成的字（「an As I turned」「men I look back」）：之後的清理步驟會拿掉，漏字檢查不算
    junk += [m.group(1) for x in out for m in [re.match(r'^([a-z]{1,4})\s+(?=[A-Z][a-z])', x)] if m]
    vision_page.lost = [w for w in _lost_words(L, out) if w not in junk]
    return out


def _lost_words(lines, paras):
    """漏字檢查：辨識到的每個字（頁首、頁碼、花紋除外）都要出現在段落裡"""
    import re
    from collections import Counter
    norm = lambda s: [w for w in re.findall(r"[a-z0-9']+", s.lower().replace('’', "'")) if len(w) > 1]
    have = Counter(norm(' '.join(paras)).__iter__())
    for p in paras:                                   # 斷字接起來的字也算
        for a, b in re.findall(r'(\w+)-(\w+)', p): have[(a + b).lower()] += 1
    joined = ''                                       # 原始的行照樣把行尾斷字接起來（peo- / ple → people）再比
    for t, *_ in lines:
        joined = joined[:-1] + t if re.search(r'[A-Za-z]-$', joined) and re.match(r'[a-z]', t) else (joined + ' ' + t)
    need = Counter(norm(joined))
    return sorted(w for w, n in need.items() if have.get(w, 0) + sum(1 for h in have if w in h and len(w) > 3) < 1)



# ---------- 數位書：照「每一行在頁面上的位置」分段（縮排、空行、置中＝標題），順序和原書一樣 ----------
# 數位書：用每個字在頁面上的位置組成「行」，再照縮排、空行、置中判斷分段和標題（跟掃描書用 Apple Vision 的做法一樣）

def page_lines(pg):
    tp = pg.get_textpage(); W, H = pg.get_size(); n = tp.count_chars()
    text = tp.get_text_range(0, n) if n else ''
    lines = []; cur = None
    for i in range(n):
        ch = text[i] if i < len(text) else ''
        if ch in '\r\n':
            if cur: lines.append(cur); cur = None
            continue
        try: l, b, r, t = tp.get_charbox(i)
        except Exception: continue
        if r - l <= 0 and ch != ' ':
            continue
        y = H - (b + t) / 2
        alnum = ch.isalnum()
        if cur and not cur.get('a') and alnum and abs(l - cur['x1']) < 15:     # 這一行到目前只有標點（開頭的引號）：用字母的高度
            cur['y'] = y; cur['a'] = True
        elif cur and (abs(y - cur['y']) > max(3, (cur['h'] or 8) * 0.6) or l < cur['x1'] - 40) and (alnum or l < cur['x1'] - 40):
            lines.append(cur); cur = None
        if cur is None: cur = {'y': y, 'x0': l, 'x1': r, 'h': t - b, 's': '', 'a': alnum}
        cur['s'] += ch; cur['x0'] = min(cur['x0'], l) if ch.strip() else cur['x0']; cur['x1'] = max(cur['x1'], r); cur['h'] = max(cur['h'], t - b)
    if cur: lines.append(cur)
    out = []
    for L in lines:
        s = re.sub(r'\s+', ' ', L['s'].replace('\ufffe', '-').replace('\u00ad', '-').replace('\u0002', '-')).strip()
        if s: L['s'] = s; out.append(L)
    out.sort(key=lambda L: (round(L['y'] / 3), L['x0']))
    merged = []
    for L in out:                                         # 同一個高度、左右接著的片段：同一行
        M = merged[-1] if merged else None
        if M and abs(L['y'] - M['y']) < max(2.5, 0.35 * (M['h'] or 8)) and L['x0'] >= M['x1'] - 2:
            M['s'] = M['s'] + ('' if M['s'].endswith('-') else ' ') + L['s']; M['x1'] = max(M['x1'], L['x1']); M['h'] = max(M['h'], L['h'])
        else: merged.append(dict(L))
    return merged, W, H

def paras_from_lines(lines, W, H):
    body = [L for L in lines if len(L['s']) > 25]
    if not body: return [(L['s'], 'h' if len(L['s']) < 60 else 'p', L['y']) for L in lines]
    left = statistics.median(sorted(L['x0'] for L in body)[:max(1, len(body) // 2)])
    right = statistics.median(sorted((L['x1'] for L in body), reverse=True)[:max(1, len(body) // 2)])
    gaps = [b['y'] - a['y'] for a, b in zip(lines, lines[1:]) if 4 <= b['y'] - a['y'] < 40]       # 行距（同一行的片段不算）
    lg = sorted(gaps)[len(gaps) // 5] if gaps else 14          # 一般行距：取比較小的那一群（對話多的頁，每行都是一段，中位數會變成段落間距）
    out = []; cur = None; kind = 'p'; prev = None; y0 = 0
    for L in lines:
        s = L['s']; ind = L['x0'] - left; rgap = right - L['x1']
        centered = ind > 20 and abs(ind - rgap) < 25 and len(s) < 80
        big = L['h'] > 1.45 * statistics.median([x['h'] for x in body]) and (L['x1'] - L['x0']) < 0.75 * (right - left)   # 字明顯比較大、而且是短短一行才算標題（括號會把一行撐高一點）
        is_head = (centered or big) and not re.search(r'[.,;:]$', s) and len(s.split()) <= 12
        gap = (L['y'] - prev['y']) if prev else 0
        indent_new = ind > 6 and (prev is None or abs(L['x0'] - prev['x0']) > 3)      # 首行縮排；條列換行後縮排的位置一樣 = 同一段
        if re.match(r'^(\d{1,3}\.\s|Part [IVXL]+\b|Chapter\b|Appendix\b|Acknowledg|Notes$|Index$|Conclusions?$)', s): indent_new = True   # 目錄的每一項
        if prev and re.match(r'^[•●▪◦]', prev['s']) and prev['x0'] + 2 < L['x0'] < prev['x0'] + 30 and not re.match(r'^[•●▪◦]', s): indent_new = False; hang = True   # 條列換行（懸掛縮排）：同一項
        else: hang = False
        if re.match(r'^[•●▪◦]\s', s): indent_new = True                                                              # 「•」開頭：新的一項
        new = (prev is None or is_head or kind == 'h' or indent_new or (gap > lg * 1.35 and not hang)
               or (prev and gap > lg * 1.12 and re.search(r'[.!?"”’)]$', prev['s']) and not hang)      # 段落間只多空一點點（小說常見）：上一句結束＋間距稍大
               or (prev and right - prev['x1'] > 40 and re.search(r'[.!?:"”’)]$', prev['s']))   # 上一行沒寫滿、句子也結束了：上一段結束
               or (prev and not hang and (prev['x1'] - prev['x0']) < 0.6 * (right - left)))               # 上一行特別短（目錄、短條列）
        if new:
            if cur: out.append((cur, kind, y0))
            cur = s; kind = 'h' if is_head else 'p'; y0 = L['y']
        else:
            cur = hy_join(cur, s) if (cur.endswith('-') and s[:1].islower()) else (cur + s) if re.search(r'\b[A-Z]{1,4}-$', cur) and s[:1].isdigit() else cur + ' ' + s   # H- / 1B → H-1B
        prev = L
    if cur: out.append((cur, kind, y0))
    return out


def line_page(pg, figs, skip_txt=(), list_items=()):
    """這一頁的內容：文字照行的位置分段；圖和表格（Docling 抓的）照它們的高度插回去；圖裡面的字不算內文"""
    lines, W, H = page_lines(pg)
    def inside(L):
        for (t, b, l, r, _) in figs:
            if t - 2 <= L['y'] <= b + 2 and L['x0'] >= l - 6 and L['x1'] <= r + 6: return True
        return False
    lines = [L for L in lines if not inside(L)]
    # 頁尾：頁面最底下、字比內文小、跟上面的內文隔一段距離的幾行（出版社版權、DOI）
    if len(lines) > 4:
        hs = sorted(L['h'] for L in lines); mh = hs[len(hs) // 2]
        ys = [L['y'] for L in lines]; gaps = sorted(b - a for a, b in zip(ys, ys[1:]) if b - a > 3); g = gaps[len(gaps) // 5] if gaps else 14
        k = len(lines)
        while k > 1 and lines[k - 1]['h'] < 0.9 * mh and lines[k - 1]['y'] > H * 0.8: k -= 1
        tail = ' '.join(L['s'] for L in lines[k:])
        if k < len(lines) and lines[k]['y'] - lines[k - 1]['y'] > g * 1.3 and re.search(r'©|\bDOI\b|\bPress\b|https?://|www\.', tail): lines = lines[:k]   # 只拿掉出版資訊（註腳是內容，留著）
    # 頁碼（只有數字）不要
    lines = [L for L in lines if not re.fullmatch(r'[\divxlcdm]{1,6}', L['s'].lower())]
    paras = paras_from_lines(lines, W, H) if lines else []
    caps = {re.sub(r'\s+', ' ', c).strip().lower() for c in skip_txt if c}
    items = []
    lkeys = [re.sub(r'\s+', ' ', x).strip()[:30] for x in list_items if len(x.strip()) >= 6]
    for t, k, y in paras:
        if t.strip().lower() in caps: continue                     # 圖說已經放進圖片裡了
        if any(t.startswith(kk) for kk in lkeys) and not re.match(r'^([•●▪◦❍○◆■□➢✓\-–—*]|\(?\d{1,2}[.)]|\(?[a-z][.)])\s', t): t = '• ' + t   # 條列：原書的「•」是畫上去的
        items.append((y, t))
    for (ft, fb, fl, fr, blk) in figs: items.append((ft - 0.5, blk))
    items.sort(key=lambda x: x[0])
    return [x for _, x in items]

def main():
    pdf, figdir = sys.argv[1], sys.argv[2]
    chunk = int(sys.argv[3]) if len(sys.argv) > 3 else 20
    start = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    os.makedirs(figdir, exist_ok=True)
    import pypdfium2
    pd = pypdfium2.PdfDocument(pdf)
    total = len(pd)
    if len(sys.argv) > 5: total = min(total, int(sys.argv[5]))        # 測試用：只做到第幾頁
    scanned = is_scanned(pd)
    use_vision = scanned and sys.platform == 'darwin' and has_ocrmac()
    print(json.dumps({'total': total, 'scanned': scanned, 'ocr': 'apple-vision' if use_vision else ''}), flush=True)

    if use_vision:                       # 掃描書：Apple Vision 一頁一頁辨識，自己分段（不經過 Docling）
        for a in range(start, total + 1, chunk):
            b = min(total, a + chunk - 1); pages = {}
            for n in range(a, b + 1):
                try:
                    pages[str(n)] = vision_page(pd[n - 1])
                    if getattr(vision_page, 'lost', None): print(json.dumps({'lost': n, 'words': vision_page.lost[:20]}), flush=True)
                except Exception as e: print(json.dumps({'warn': 'page %d: %s' % (n, e)}), flush=True)
            print(json.dumps({'from': a, 'to': b, 'pages': pages}, ensure_ascii=False), flush=True)
        return

    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, OcrMacOptions
    from docling_core.types.doc import PictureItem, TableItem, TextItem

    o = PdfPipelineOptions()
    if use_vision:
        # 掃描書：PDF 裡原本那層 OCR 文字常常很爛（norm.alcy、v.rere、W3S），用 Mac 內建的 Apple Vision 整頁重新辨識
        o.do_ocr = True
        o.ocr_options = OcrMacOptions(force_full_page_ocr=True, lang=['en-US'])
    else:
        o.do_ocr = False                # 數位書：直接用 PDF 裡的文字（最準）
    o.generate_picture_images = True
    o.generate_table_images = True
    o.images_scale = 2.0
    conv = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=o)})
    SKIP = {'page_header', 'page_footer'}

    for a in range(start, total + 1, chunk):
        b = min(total, a + chunk - 1)
        try:
            doc = conv.convert(pdf, page_range=(a, b)).document
        except Exception as e:
            print(json.dumps({'from': a, 'to': b, 'error': str(e)[:300]}), flush=True); continue
        caps = set()
        for it, _ in doc.iterate_items():
            if isinstance(it, (PictureItem, TableItem)):
                c = it.caption_text(doc)
                if c: caps.add(c.strip())
        if not use_vision and os.environ.get('RV_DOCLING_TEXT') != '1':        # 數位書：文字照行的位置排，Docling 只負責圖表
            figs, capt, lists, k = {}, {}, {}, 0
            dseq = {}                                   # Docling 自己的排法（文字＋圖表，照它的順序），等一下每頁比較用
            for it, _ in doc.iterate_items():
                if not getattr(it, 'prov', None): continue
                lab = getattr(getattr(it, 'label', None), 'value', '')
                if lab == 'list_item': lists.setdefault(it.prov[0].page_no, []).append(it.text or '')
                if isinstance(it, TextItem) and not isinstance(it, (PictureItem, TableItem)) and lab not in SKIP:
                    t = (it.text or '').strip()
                    if t and lab != 'caption':
                        if lab == 'list_item' and not re.match(r'^([•●▪◦❍○◆■□➢✓\-–—*]|\(?\d{1,2}[.)]|\(?[a-z][.)])\s', t): t = '• ' + t
                        dseq.setdefault(it.prov[0].page_no, []).append(t)
                if not isinstance(it, (PictureItem, TableItem)): continue
                pno = it.prov[0].page_no
                img = it.get_image(doc)
                if img is None or min(img.size) < 40: continue
                kind = 'table' if isinstance(it, TableItem) else 'fig'
                name = 'p%s_%s%d.png' % (pno, 't' if kind == 'table' else '', k); k += 1
                img.save(os.path.join(figdir, name))
                cap = (it.caption_text(doc) or '').strip()
                blk = {'t': kind, 'src': name, 'w': img.size[0], 'h': img.size[1], 'cap': cap}
                if kind == 'table':
                    rows = [[(c.text or '').strip() for c in row] for row in it.data.grid]
                    if rows and any(any(x for x in r) for r in rows): blk['rows'] = rows
                try:
                    ph = doc.pages[pno].size.height
                    bb = it.prov[0].bbox.to_top_left_origin(ph)
                    figs.setdefault(pno, []).append((bb.t, bb.b, bb.l, bb.r, blk))
                except Exception:
                    figs.setdefault(pno, []).append((0, 0, 0, 0, blk))
                if cap: capt.setdefault(pno, []).append(cap)
                dseq.setdefault(pno, []).append(blk)
            pages = {}
            for n in range(a, b + 1):
                try:
                    lp_items = line_page(pd[n - 1], figs.get(n, []), capt.get(n, []), lists.get(n, []))
                    dl_items = dseq.get(n, [])
                    # 每一頁選比較準的排法：拿 PDF 本身的文字順序當標準，看「照行的位置排」和「Docling 排」哪個跟它比較一致
                    import difflib
                    W = lambda xs: re.findall(r'[a-z0-9]+', ' '.join(x for x in xs if isinstance(x, str)).lower())
                    ref = re.findall(r'[a-z0-9]+', pd[n - 1].get_textpage().get_text_range().lower())
                    def sc(xs):
                        w = W(xs)
                        if not ref or not w: return 0
                        sm = difflib.SequenceMatcher(None, ref, w, autojunk=False)
                        return sum(m.size for m in sm.get_matching_blocks()) / max(len(ref), len(w))
                    a_, b_ = sc(lp_items), sc(dl_items)
                    pages[str(n)] = dl_items if (dl_items and b_ > a_ + 0.03) else lp_items
                except Exception as e: print(json.dumps({'warn': 'page %d: %s' % (n, e)}), flush=True)
            print(json.dumps({'from': a, 'to': b, 'pages': pages, 'lineMode': True}, ensure_ascii=False), flush=True)
            continue
        pages, k = {}, 0
        for it, _ in doc.iterate_items():
            if not getattr(it, 'prov', None): continue
            pg = str(it.prov[0].page_no)
            lab = getattr(getattr(it, 'label', None), 'value', '')
            if lab in SKIP: continue
            if isinstance(it, (PictureItem, TableItem)):
                img = it.get_image(doc)
                if img is None or min(img.size) < 40: continue           # 太小的（裝飾線、小圖示）不要
                kind = 'table' if isinstance(it, TableItem) else 'fig'
                name = 'p%s_%s%d.png' % (pg, 't' if kind == 'table' else '', k); k += 1
                img.save(os.path.join(figdir, name))
                blk = {'t': kind, 'src': name, 'w': img.size[0], 'h': img.size[1], 'cap': (it.caption_text(doc) or '').strip()}
                if kind == 'table':
                    rows = [[(c.text or '').strip() for c in row] for row in it.data.grid]
                    if rows and any(any(x for x in r) for r in rows): blk['rows'] = rows
                pages.setdefault(pg, []).append(blk)
            elif isinstance(it, TextItem):
                t = (it.text or '').strip()
                if not t or (lab == 'caption' and t in caps): continue   # 圖說已經放進圖片裡了
                if lab in ('section_header', 'title'): t = t            # 標題照樣當一段（之後也許另外標示）
                if lab == 'list_item' and not re.match(r'^([•●▪◦❍○◆■□➢✓\-–—*]|\(?\d{1,2}[.)]|\(?[a-z][.)])\s', t): t = '• ' + t   # 條列：原書的「•」是畫上去的，補回來
                pages.setdefault(pg, []).append(t)
        print(json.dumps({'from': a, 'to': b, 'pages': pages}, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
