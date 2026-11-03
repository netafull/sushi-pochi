#!/usr/bin/env python3
"""回転寿司4社の公式アレルゲン(カロリー)PDFの表を読む純粋関数(ネットワーク・pdfminer には触らない)。

入力は pdf_geometry.extract_geometry(data, with_chars=True) が返す、ページごとの文字行(texts)と文字(chars)。
4社とも「1行1メニュー、列がアレルゲン品目、セルに記号」の表だが、細部が違うので設定(SPECS)で吸収する。

  ・スシロー  メニュー名の列 + 28列(● 使用 / ○ 工場で製造ラインを共有)。空欄＝記号なし。カロリー無し。
  ・はま寿司  名前 + 熱量(kcal・一皿) + 28列(● 使用 / - 不使用 / △ 同じラインで製造)。カロリー有り(公式FAQの「カロリー非公開」は誤り)。
  ・くら寿司  名前 + カロリー + 該当なし + 26列(● 含む / ▲ 同じラインで製造)。空欄＝記号なし。
  ・かっぱ寿司 名前 + 28列(● 有り / - 無し)。カロリー無し(カロリーは menulist.json にある)。

読み取り方:
  1. メニュー名の左端x・記号のx・(あれば)カロリーのxの範囲を設定で決め、表の見出し(縦書きで1文字ずつ並ぶもの・
     横書きのラベル)のx位置で「列」を作る。品目名は見出しの文字を上から下へつなげたもの。
  2. 各メニュー名と同じ高さにある記号を、一番近い列に割り当てる。
  3. 検証(validate_table)に通らない読み取りは、呼び出し側(pdf_update.py)が採用しない(既存の値は変えない)。

規約上、PDFの表そのものは保存しない。サイトで記録している商品に名前が一致した行の値だけを商品データに入れる。
"""

from __future__ import annotations

import re
import statistics
import unicodedata

# 見出し行の文字(メニュー名の列にある「商品名」など)は、メニューではない
NAME_SKIP = re.compile(r"^(商品名|メニュー名|メニュー名称|品\s*名|分類|区分|メニューカテゴリ)$")


def cx(t) -> float:
    return (t[0] + t[2]) / 2


def cy(t) -> float:
    return (t[1] + t[3]) / 2


# NFKC では直らない部首の字(PDFのフォントが部首の字形を使っている)。見つけたらここに足す
_RADICALS = str.maketrans({"⻨": "麦", "⻑": "長", "⻄": "西", "⻤": "鬼", "⻩": "黄", "⻘": "青", "⻭": "歯", "⻲": "亀"})


def nfkc(s: str | None) -> str:
    return unicodedata.normalize("NFKC", s or "").translate(_RADICALS)


def norm(s: str | None) -> str:
    """商品名の照合用: NFKC(互換部首の「⾚⾝」なども普通の字に直る)・空白と記号を除き小文字にする。"""
    s = nfkc(s)
    s = re.sub(r"[\s　「」『』\"“”'’・＜＞<>()（）【】\[\]～〜~!！?？\-ー―—－:：/／,、。.&＆®™©*＊★☆♪]+", "", s)
    return s.lower()


# ---------------------------------------------------------------------------
# 設定
# ---------------------------------------------------------------------------

SPECS = {
    "sushiro": {
        "label": "スシロー アレルギー情報",
        "name_x": (100.0, 250.0), "mark_x0": 250.0, "kcal_x": None,
        "marks": "●○", "contains": "●", "partial": "○",
        "partial_meaning": "工場で製造ラインを共有しています",
        "header": "stacked", "header_h": 400.0, "drop_digits": False, "skip": None,
        "blank_ok": True,
        "updated": [r"(\d{4})/(\d{1,2})/(\d{1,2})\s*更新"],
        "min_rows": 150, "n_cols": (24, 32), "min_marked_rate": 0.4,
    },
    "hamazushi": {
        "label": "はま寿司 アレルゲン一覧表(店内)",
        "name_x": (80.0, 262.0), "mark_x0": 280.0, "kcal_x": (262.0, 280.0),
        "marks": "●-△", "contains": "●", "partial": "△",
        "partial_meaning": "原材料の製造工場で、そのアレルゲンを含む製品を同じラインで製造しています",
        "header": "stacked", "header_h": 400.0, "drop_digits": True, "skip": r"特定原材料",
        "blank_ok": False,
        "updated": [r"更新日\s*(\d{4})/(\d{1,2})/(\d{1,2})"],
        "min_rows": 200, "n_cols": (24, 32), "min_marked_rate": 0.9,
    },
    "kurasushi": {
        "label": "くら寿司 アレルゲン・カロリー情報",
        "name_x": (240.0, 595.0), "mark_x0": 625.0, "kcal_x": (595.0, 625.0),
        "marks": "●▲", "contains": "●", "partial": "▲",
        "partial_meaning": "アレルギー物質を含む製品と同じラインで製造しています",
        "header": "stacked", "header_h": 600.0, "drop_digits": False, "skip": None,
        "blank_ok": True,
        "updated": [r"(\d{4})年(\d{1,2})月(\d{1,2})日現在"],
        "min_rows": 80, "n_cols": (22, 32), "min_marked_rate": 0.4,
    },
    "kappasushi": {
        "label": "かっぱ寿司 メニュー別アレルゲン情報",
        "name_x": (100.0, 215.0), "mark_x0": 220.0, "kcal_x": None,
        "marks": "●-", "contains": "●", "partial": None,
        "partial_meaning": None,
        "header": "labels", "header_h": 45.0, "drop_digits": True, "skip": r"特定原材料|準ずる|カシューナッツが",
        "blank_ok": False,
        "updated": [r"商品開発部門確認日\s*[:：]\s*(\d{4})/(\d{1,2})/(\d{1,2})"],
        "min_rows": 150, "n_cols": (24, 32), "min_marked_rate": 0.9,
    },
}

REQUIRED_ALLERGENS = ["卵", "小麦", "えび", "かに"]  # 列名に必ず含まれるはずの品目(NFKC・かな表記ゆれは見ない)


# ---------------------------------------------------------------------------
# 表の読み取り
# ---------------------------------------------------------------------------

def _cluster(vals: list[float], tol: float) -> list[list[float]]:
    out: list[list[float]] = []
    for v in sorted(vals):
        if out and v - out[-1][-1] <= tol:
            out[-1].append(v)
        else:
            out.append([v])
    return out


def _clean_col_name(s: str) -> str:
    s = nfkc(s)
    s = re.sub(r"[（(][^）)]*[）)]", "", s)  # 「落花生（ピーナッツ）」→「落花生」
    return s.strip()


def _updated(pages: list[dict], patterns: list[str]) -> str | None:
    """PDFに書かれた更新日・確認日のうち、いちばん新しいもの(かっぱ寿司のPDFはページごとに確認日が違い、
    先頭ページは古い版のままなので、最新のものを版とする)。"""
    found = []
    for p in pages:
        for t in p["texts"]:
            s = nfkc(t[4])
            for pat in patterns:
                m = re.search(pat, s)
                if m:
                    found.append(f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}")
    return max(found) if found else None


def _page_table(page: dict, spec: dict) -> dict | None:
    """1ページ分 -> {"columns": [...], "rows": [...]}。表が無いページは None。"""
    texts, chars = page["texts"], page.get("chars") or []
    mx0 = spec["mark_x0"]
    marks = [c for c in chars if c[4] in spec["marks"] and cx(c) >= mx0]
    nx0, nx1 = spec["name_x"]
    names = [t for t in texts if nx0 <= t[0] < nx1 and t[4] not in spec["marks"] and not NAME_SKIP.match(nfkc(t[4]).strip())]
    if not marks or not names:
        return None
    # 記号を3つ(無ければ2つ)以上持つメニュー名のうち一番上の行 = 表の最初の行(凡例や注記の行を取り違えない)
    def nmarks(n, tol=4.0):
        return sum(1 for c in marks if abs(cy(c) - cy(n)) <= tol)

    anchored: list = []
    for need in (3, 2):  # 記号が疎なページ(ドリンク・デザートだけ等)は、条件を緩めて探す
        anchored = [n for n in names if nmarks(n) >= need]
        if anchored:
            break
    if not anchored:
        return None
    first_y = max(cy(n) for n in anchored)
    # 見出しの文字: 記号の列の領域にあって、最初の行より上にある、記号ではない文字
    skip = re.compile(spec["skip"]) if spec.get("skip") else None
    head = []
    for t in texts:
        if cx(t) < mx0 or t[4] in spec["marks"] or cy(t) <= first_y + 2 or cy(t) > first_y + spec["header_h"]:
            continue
        s = nfkc(t[4]).strip()
        if not s:
            continue
        if spec["header"] == "stacked" and len(s) != 1:
            continue
        if spec["header"] == "labels" and len(s) > 10:
            continue
        if spec["drop_digits"] and re.fullmatch(r"\d{1,2}", s):
            continue
        if skip and skip.search(s):
            continue
        head.append(t)
    head = [t for t in head if t[4].strip()]
    if not head:
        return None
    header_bottom = min(cy(t) for t in head)
    tol_x = 5.0 if spec["header"] == "stacked" else 8.0
    groups = _cluster([cx(t) for t in head], tol_x)
    centers = [statistics.mean(g) for g in groups]
    columns = []
    for c, g in zip(centers, groups):
        lo, hi = min(g), max(g)
        col_texts = sorted((t for t in head if lo - 0.01 <= cx(t) <= hi + 0.01), key=lambda t: -cy(t))
        columns.append(_clean_col_name("".join(t[4] for t in col_texts)))
    gaps = [b - a for a, b in zip(centers, centers[1:])]
    step = statistics.median(gaps) if gaps else 20.0
    # 行: 見出しより下のメニュー名(1行に複数の文字行があれば左のものを名前とみなす)
    row_names = sorted((n for n in names if cy(n) < header_bottom - 1), key=lambda n: -cy(n))
    ys = [cy(n) for n in row_names]
    row_gap = statistics.median([a - b for a, b in zip(ys, ys[1:]) if a - b > 1]) if len(ys) > 2 else 12.0
    tol_y = max(2.5, row_gap * 0.42)
    rows = []
    for n in row_names:
        y = cy(n)
        cells: list[str | None] = [None] * len(columns)
        stray = 0
        for c in marks:
            if abs(cy(c) - y) > tol_y:
                continue
            k = min(range(len(centers)), key=lambda i: abs(centers[i] - cx(c)))
            if abs(centers[k] - cx(c)) <= step * 0.5:
                cells[k] = c[4]
            else:
                stray += 1
        kcal = None
        if spec["kcal_x"]:
            kx0, kx1 = spec["kcal_x"]
            cand = [t for t in texts if kx0 <= t[0] < kx1 and abs(cy(t) - y) <= tol_y
                    and re.fullmatch(r"\d+(?:\.\d+)?", unicodedata.normalize("NFKC", t[4]).strip())]
            if len(cand) == 1:
                kcal = float(unicodedata.normalize("NFKC", cand[0][4]).strip())
        rows.append({"name": n[4].strip(), "kcal": kcal, "cells": cells, "stray": stray,
                     "marked": sum(1 for x in cells if x), "columns": columns})
    return {"columns": columns, "rows": rows}


def parse_table(pages: list[dict], chain_key: str) -> dict:
    """-> {"updated","column_sets":[[...]],"rows":[{"name","kcal","cells","marked","columns"}],"pages_total","pages_table","problems"}

    ページごとに品目の並びが違うPDFがある(かっぱ寿司は2026-06-23に掲載順を改定した版と古い版が同居する)。
    行は自分のページの品目の並び(columns)を持つ。"""
    spec = SPECS[chain_key]
    rows: list[dict] = []
    column_sets: list[list[str]] = []
    pages_table = 0
    for p in pages:
        pt = _page_table(p, spec)
        if pt is None:
            continue
        pages_table += 1
        if pt["columns"] not in column_sets:
            column_sets.append(pt["columns"])
        rows.extend(pt["rows"])
    return {"updated": _updated(pages, spec["updated"]), "column_sets": column_sets, "rows": rows,
            "pages_total": len(pages), "pages_table": pages_table, "problems": []}


def validate_table(chain_key: str, parsed: dict) -> str | None:
    """読み取り結果が妥当か。問題があれば理由、なければ None。構造が変わったPDFで誤った値を出さないための門。"""
    spec = SPECS[chain_key]
    if parsed.get("pages_table", 0) == 0:
        return "表の見出しが1ページも見つかりません(PDFの構造が変わった疑い)"
    rows = parsed["rows"]
    if len(rows) < spec["min_rows"]:
        return f"読み取れた行が少なすぎます({len(rows)}行 < {spec['min_rows']})"
    lo, hi = spec["n_cols"]
    for cols in parsed["column_sets"]:
        n = len(cols)
        if not (lo <= n <= hi):
            return f"品目の列数が想定と違います({n}列)"
        names = [norm(c) for c in cols]
        missing = [a for a in REQUIRED_ALLERGENS if not any(norm(a) in c for c in names)]
        if missing:
            return f"必須の品目の列が見つかりません: {missing}"
        if len(set(names)) != len(names):
            return "品目名が重複しています(列の割り当てが崩れた疑い)"
    rate = sum(1 for r in rows if r["marked"] > 0) / len(rows)
    if rate < spec["min_marked_rate"]:
        return f"記号を読み取れた行の割合が低すぎます({rate:.0%})"
    if not spec["blank_ok"]:
        # 空欄の行が無い表(- で埋める表): 記号が全部そろわない行が多いなら列の割り当てを疑う
        full = sum(1 for r in rows if r["marked"] == len(r["columns"])) / len(rows)
        if full < 0.8:
            return f"記号がそろっている行の割合が低すぎます({full:.0%})"
    if not parsed.get("updated"):
        return "更新日(版)が見つかりません"
    if spec["kcal_x"]:
        kr = sum(1 for r in rows if r["kcal"] is not None) / len(rows)
        if kr < 0.6:
            return f"カロリーを読み取れた行の割合が低すぎます({kr:.0%})"
    return None


# ---------------------------------------------------------------------------
# 照合
# ---------------------------------------------------------------------------

_NOT_HOKKAIDO_ONLY = re.compile(r"^[（(]\s*北海道\s*(限定|のみ)\s*[）)]")


def name_keys(name: str) -> list[str]:
    """商品名の照合キー。ゆるくなる順(完全一致 → 先頭の注記・末尾の数量を除いたもの)。短すぎるものは使わない。"""
    n = nfkc(name)
    n = re.sub(r"\s+", " ", n).strip()
    variants = [n]
    v = re.sub(r"^(?:\[[^\]]*\]|[(（][^)）]*[)）]|【[^】]*】)\s*", "", n)
    variants.append(v)
    v2 = re.sub(r"\s*(?:\d+\s*貫|[(（]\s*[一二三四五六七八九1-9]\s*[貫皿個本枚][)）])\s*$", "", v)
    variants.append(v2)
    out: list[str] = []
    for x in variants:
        k = norm(x)
        if len(k) >= 2 and k not in out:
            out.append(k)
    return out


def rows_for_name(rows: list[dict], name: str) -> list[dict] | None:
    """商品名に対応する行を返す。同名の行が別の記号で複数あって決められないとき・無いときは None。
    曖昧な部分一致はしない。商品側の完全な名前が、行側の「注記を除いた名前」と一致するところまでは許す。"""
    pk = name_keys(name)
    if not pk:
        return None
    row_full = [(norm(r["name"]), r) for r in rows]
    # 1) 完全一致
    hit = [r for k, r in row_full if k == pk[0]]
    if not hit:
        # 2) 商品名のゆるいキー(注記・数量を除いたもの)が、行の名前のどれかのキーと一致
        row_loose = [(name_keys(r["name"]), r) for r in rows]
        for key in pk:
            hit = [r for ks, r in row_loose if key in ks]
            if hit:
                break
    if not hit:
        return None
    # 北海道限定の行は、全国の商品には使わない
    nation = [r for r in hit if not _NOT_HOKKAIDO_ONLY.match(nfkc(r["name"]))]
    hit = nation or hit
    if len({tuple(r["cells"]) for r in hit}) > 1:
        return None
    return hit


def allergens_from_row(chain_key: str, row: dict) -> dict:
    spec = SPECS[chain_key]
    contains, partial = [], []
    for name, mark in zip(row["columns"], row["cells"]):
        if mark is None:
            continue
        if mark == spec["contains"]:
            if name not in ("該当なし",):
                contains.append(name)
        elif spec.get("partial") and mark == spec["partial"]:
            partial.append({"name": name, "mark": mark})
    return {"contains": contains, "partial": partial, "partial_meaning": spec.get("partial_meaning")}
