"""Readiverse：用 Docling 解析一本 PDF（在 Readiverse 自己的 Python 3.12 環境裡執行，由 server.py 叫起來）

用法：python docling_worker.py <book.pdf> <圖片資料夾> [每次幾頁=20] [從第幾頁開始=1]
每處理完一批頁，就在 stdout 印一行 JSON：
  {"total": 總頁數}
  {"from": 1, "to": 20, "pages": {"1": [段落或圖表…], …}}
段落是字串；圖片是 {"t":"fig","src":"p12_0.png","cap":"Figure 6.2 …"}；
表格是 {"t":"table","src":"p90_t0.png","rows":[[格, 格…], …],"cap":"Table 6.1 …"}
"""
import json, os, sys

def main():
    pdf, figdir = sys.argv[1], sys.argv[2]
    chunk = int(sys.argv[3]) if len(sys.argv) > 3 else 20
    start = int(sys.argv[4]) if len(sys.argv) > 4 else 1
    os.makedirs(figdir, exist_ok=True)
    import pypdfium2
    total = len(pypdfium2.PdfDocument(pdf))
    print(json.dumps({'total': total}), flush=True)

    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling_core.types.doc import PictureItem, TableItem, TextItem

    o = PdfPipelineOptions()
    o.do_ocr = False                    # 用 PDF 裡現成的文字（掃描書已經有 OCR 層，重做反而會把字黏在一起）
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
