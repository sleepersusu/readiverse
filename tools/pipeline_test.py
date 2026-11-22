#!/usr/bin/env python3
"""Readiverse 解析流程測試：拿幾本 PDF 跑完整的解析流程（跟 App 匯入時一模一樣），自動檢查常見問題。
不會動到書庫。用 Docling 環境的 Python 執行：

  ~/.readiverse/docling-venv/bin/python tools/pipeline_test.py 書1.pdf 書2.pdf ... [--pages 1-40] [--out 資料夾]

檢查項目：亂碼頁、段落開頭的亂碼、殘留的頁首、章名、跨頁沒接上的段落、句子被切斷、超長段落、空白頁
"""
import json, os, re, subprocess, sys, time, tempfile
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import server as S                       # clean_pages、garble 跟 App 用的是同一份


def run_worker(pdf, rng):
    """跟 App 一樣：呼叫 docling_worker.py，收每一批的結果"""
    figdir = tempfile.mkdtemp(prefix='rv_figs_')
    a, b = rng
    p = subprocess.Popen([sys.executable, os.path.join(HERE, 'docling_worker.py'), pdf, figdir, '20', str(a)],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    total, info, pages = 0, {}, {}
    for line in p.stdout:
        try: j = json.loads(line)
        except ValueError: continue
        if 'total' in j: total = j['total']; info = j; b = min(b, total)
        if 'pages' in j: pages.update(j['pages'])
        if j.get('to', 0) >= b: break
    p.kill()                              # 要的頁數拿到就停（不用整本跑完）
    return total, info, pages


def check(pages, first):
    """回傳問題清單 [(頁碼, 種類, 內容)]"""
    probs = []
    for i, pg in enumerate(pages):
        no = first + i
        T = [x for x in pg if S.is_text(x)]
        sc, nb = S.garble(pg)
        if sc > S.QUALITY_THR and nb >= 3: probs.append((no, '亂碼', '%.0f%% 的字有問題' % (sc * 100)))
        for x in T:
            if (re.match(r'^[a-z]{1,4}\s+[A-Z]', x) or re.match(r'^[^\w"“‘(\'\[—–]', x)) and not re.fullmatch(r'[\d\s-]+', x) and not re.match(r'^[-–—]\s*[A-Z]', x): probs.append((no, '段落開頭有雜訊', x[:60]))
            if len(x) > 3000: probs.append((no, '超長段落（可能接錯）', x[:60] + '…'))
            m = re.search(r'[.!?"”’)]\s?\d{1,3}(?:,\s?\d{1,3})*(?=\s+[A-Z"“‘]|\s*$)', x)
            if m: probs.append((no, '註腳編號沒拿掉', x[max(0, m.start() - 25):m.end() + 10]))
            if re.search(r'[a-z]{3,}-(?:and|but|or|one|the|a|which|that|who|not|even|if)\s', x): probs.append((no, '破折號變成連字號', re.search(r'\S+-(?:and|but|or|one|the|a|which|that|who|not|even|if)\s', x).group(0)))
        for k in range(len(T) - 1):
            if re.search(r'[A-Za-z]-$', T[k]) and re.match(r'[a-z]', T[k + 1]): probs.append((no, '斷字沒接上', T[k][-30:] + ' ‖ ' + T[k + 1][:20]))
        for k in range(len(T) - 1):
            a, b2 = T[k].strip(), T[k + 1].strip()
            if len(a) > 60 and not re.search(r'[.!?:;"”’)\]—–]$', a) and re.match(r'[a-z]', b2):
                probs.append((no, '句子被切成兩段', a[-30:] + ' ‖ ' + b2[:30]))
        if i + 1 < len(pages):
            N = [x for x in pages[i + 1] if S.is_text(x)]
            if T and N and not re.search(r'[.!?:;"”’)\]—–]$', T[-1].strip()) and re.match(r'[a-z]', N[0]):
                probs.append((no, '跨頁段落沒接上', T[-1][-30:] + ' ‖ ' + N[0][:30]))
    return probs


def main():
    args = sys.argv[1:]; rng = None; out = None; pdfs = []
    while args:
        a = args.pop(0)
        if a == '--pages': x, y = args.pop(0).split('-'); rng = (int(x), int(y))
        elif a == '--out': out = args.pop(0)
        else: pdfs.append(a)
    allp = 0
    for pdf in pdfs:
        t = time.time()
        total, info, raw = run_worker(pdf, rng or (1, 10 ** 6))
        a, b = rng or (1, total)
        b = min(b, total)
        pages = [raw.get(str(n), []) for n in range(a, b + 1)]
        pages = S.clean_pages(pages)
        probs = check(pages, a)
        heads = [(a + i, x) for i, pg in enumerate(pages) for k, x in enumerate(pg)
                 if S.is_text(x) and k == 0 and len(x) <= 60 and not re.search(r'[.!?,;:]$', x)]
        print('\n==== %s  (%s, 第 %d-%d 頁, %.0f 秒)' % (os.path.basename(pdf), '掃描書・' + (info.get('ocr') or 'PDF 文字') if info.get('scanned') else '數位書・Docling', a, b, time.time() - t))
        print('  段落 %d、圖表 %d、空白頁 %d' % (sum(1 for pg in pages for x in pg if S.is_text(x)), sum(1 for pg in pages for x in pg if not S.is_text(x)), sum(1 for pg in pages if not pg)))
        print('  頁首開頭像章名的：' + '、'.join('p%d「%s」' % h for h in heads[:12]) + (' …' if len(heads) > 12 else ''))
        kinds = {}
        for no, k, txt in probs: kinds.setdefault(k, []).append((no, txt))
        if not probs: print('  ✓ 沒有發現問題')
        for k, L in kinds.items():
            print('  ✗ %s：%d 處' % (k, len(L)))
            for no, txt in L[:5]: print('      p%d  %s' % (no, txt))
        allp += len(probs)
        if out:
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, os.path.basename(pdf) + '.json'), 'w', encoding='utf-8') as f: json.dump(pages, f, ensure_ascii=False, indent=1)
    print('\n總共 %d 個問題' % allp)


if __name__ == '__main__':
    main()
