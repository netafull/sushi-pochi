#!/usr/bin/env python3
"""公式PDFの文字と罫線を座標つきで取り出す(pdfminer.six)。ファストフードポチの pdf_nutrition.py から
extract_geometry だけを移したもの。表の解釈は pdf_tables.py。

pdfminer.six は Python 標準ライブラリではない(requirements-pdf.txt でバージョンを固定)。無ければ ImportError。
"""

from __future__ import annotations

import io
import logging
import re
import sys


def extract_geometry(data: bytes, with_chars: bool = False) -> list[dict]:
    """PDFのバイト列 -> ページごとの {"texts","rules","chars"?}。

    texts: [x0,y0,x1,y1,文字列](文字行ごと。PDFの座標系で、yは下が小さい)
    rules: 罫線として使われている細い矩形・線 [x0,y0,x1,y1]
    chars: 1文字ずつ [x0,y0,x1,y1,文字](with_chars=True のときだけ。縦書き風の見出しや、1つの文字行に
           複数の列の記号が詰まって出るセルを、文字のxで列に割り当てるために使う)
    """
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LAParams, LTChar, LTLine, LTRect, LTTextLine
        from pdfminer.pdfparser import PDFSyntaxError
        from pdfminer.psparser import PSException
    except ImportError as e:  # 呼び出し側が警告を出してPDFの読み取りを飛ばす
        raise ImportError("pdfminer.six が見つかりません(pip install -r requirements-pdf.txt)") from e
    # フォントの色指定への警告がページ数ぶん出て、ログが埋まるため
    logging.getLogger("pdfminer").setLevel(logging.ERROR)

    def read(buf: bytes) -> list[dict]:
        pages = []
        for page in extract_pages(io.BytesIO(buf), laparams=LAParams(char_margin=1.0)):
            texts: list = []
            rules: list = []
            chars: list = []

            def walk(o) -> None:
                if isinstance(o, LTTextLine):
                    t = o.get_text().strip()
                    if with_chars:
                        for c in o:
                            if isinstance(c, LTChar) and c.get_text().strip():
                                chars.append([round(c.x0, 1), round(c.y0, 1), round(c.x1, 1), round(c.y1, 1), c.get_text()])
                    if t:
                        texts.append([round(o.x0, 1), round(o.y0, 1), round(o.x1, 1), round(o.y1, 1), t])
                elif isinstance(o, (LTRect, LTLine)):
                    w, h = o.x1 - o.x0, o.y1 - o.y0
                    if min(w, h) < 4 and max(w, h) > 10:
                        rules.append([round(o.x0, 1), round(o.y0, 1), round(o.x1, 1), round(o.y1, 1)])
                elif hasattr(o, "__iter__"):
                    for c in o:
                        walk(c)

            walk(page)
            d = {"texts": texts, "rules": rules, "w": round(page.width, 1), "h": round(page.height, 1)}
            if with_chars:
                d["chars"] = chars
            pages.append(d)
        return pages

    try:
        return read(data)
    except (PDFSyntaxError, PSException) as first:
        # 吉野家PDFは2026-10-28に追記保存され、pdfminerが最後のXRefを読めず落ちた。追記を1つずつ外して読む
        eofs = [m.end() for m in re.finditer(rb"%%EOF", data)]
        for end in reversed(eofs[:-1]):
            try:
                pages = read(data[:end])
            except (PDFSyntaxError, PSException):
                continue
            print(f"[pdf] 追記部分({len(data) - end}バイト)を外して読み直した", file=sys.stderr)
            return pages
        raise first
