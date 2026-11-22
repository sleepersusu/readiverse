"""Readiverse：用 Docling 解析一本 PDF（在 Readiverse 自己的 Python 3.12 環境裡執行，由 server.py 叫起來）

用法：python docling_worker.py <book.pdf> <圖片資料夾> [每次幾頁=20] [從第幾頁開始=1]
每處理完一批頁，就在 stdout 印一行 JSON：
  {"total": 總頁數}
  {"from": 1, "to": 20, "pages": {"1": [段落或圖表…], …}}
段落是字串；圖片是 {"t":"fig","src":"p12_0.png","cap":"Figure 6.2 …"}；
表格是 {"t":"table","src":"p90_t0.png","rows":[[格, 格…], …],"cap":"Table 6.1 …"}
"""
import json, os, sys

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
    L = [(t.strip(), x, y, w, h) for t, c, (x, y, w, h) in ocrmac.OCR(img, recognition_level='accurate', language_preference=['en-US']).recognize() if t.strip()]
    if not L: return []
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
    out, cur, prev = [], None, None
    for k, (t, x, y, w, h) in enumerate(L):
        head = k in heads or (h > medh * 1.3 and w < maxw * 0.75 and len(t.split()) <= 10 and not re.search(r'[.!?,;]$', t))
        new = (prev is None or head or prev['head'] or x > left + 0.025
               or (prev['y'] - (y + h)) > medh * 1.2
               or (prev['w'] < maxw * 0.85 and re.search(r'[.!?:"”’]$', prev['t'])))
        if new:
            if cur is not None: out.append(cur)
            if prev is not None and prev['head'] and not head:
                t = re.sub(r'^[a-z]{1,4}\s+(?=[A-Z])', '', t)       # 花紋、首字放大被辨識成的亂碼
            t = re.sub(r'^([A-Z])\s+(?=\1[a-z])', '', t)            # 「A As my old professor」
            cur = t
        elif re.search(r'[A-Za-z]-$', cur) and re.match(r'[a-z]', t): cur = cur[:-1] + t
        else: cur = cur + ' ' + t
        prev = {'t': t, 'y': y, 'w': w, 'head': head}
    if cur is not None: out.append(cur)
    return out


def main():
    pdf, figdir = sys.argv[1], sys.argv[2]
    chunk = int(sys.argv[3]) if len(sys.argv) > 3 else 20
    start = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    os.makedirs(figdir, exist_ok=True)
    import pypdfium2
    pd = pypdfium2.PdfDocument(pdf)
    total = len(pd)
    scanned = is_scanned(pd)
    use_vision = scanned and sys.platform == 'darwin' and has_ocrmac()
    print(json.dumps({'total': total, 'scanned': scanned, 'ocr': 'apple-vision' if use_vision else ''}), flush=True)

    if use_vision:                       # 掃描書：Apple Vision 一頁一頁辨識，自己分段（不經過 Docling）
        for a in range(start, total + 1, chunk):
            b = min(total, a + chunk - 1); pages = {}
            for n in range(a, b + 1):
                try: pages[str(n)] = vision_page(pd[n - 1])
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
                pages.setdefault(pg, []).append(t)
        print(json.dumps({'from': a, 'to': b, 'pages': pages}, ensure_ascii=False), flush=True)

if __name__ == '__main__':
    main()
