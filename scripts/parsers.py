#!/usr/bin/env python3
"""スシロー・くら寿司・はま寿司・かっぱ寿司の公式データのパーサ。

ネットワークには触らない純粋関数だけを置く(合成フィクスチャでテストする)。
公式のHTML/JSONの構造が変わったら、まずここが壊れる。壊れ方は2通りに分ける:
  ・構造が想定と違う(キーが無い・型が違う・商品ブロックが1つも無い) -> ValueError。呼び出し側は
    「構造変更の疑い」として list_failed 扱いにし、終了判定をしない。
  ・構造は正しいが該当の商品が0件 -> 空のリストを返す。

規約: 4社とも複製・転載を禁じているため、ここで取り出すのは事実(商品名・価格・日付・カロリー・
アレルゲン)だけ。説明文・画像(のURL)は、読み取りもしない。
(くら寿司のフェア判定だけは、画像ファイル名を「判定の手がかりとして」読むが、値は返さない。)
"""


from __future__ import annotations

import datetime
import hashlib
import html as html_lib
import json
import re
import unicodedata

# ---------------------------------------------------------------------------
# 共通
# ---------------------------------------------------------------------------


def clean_text(s: str | None) -> str:
    """HTMLタグ(<br>は空白扱い)を除き、実体参照を戻し、空白を整える。"""
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_lib.unescape(s)
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t\r\n　]+", " ", s)
    return s.strip()


def strip_html(s: str | None) -> str:
    """発売日の照合用に、記事全体を平文にする(スクリプト・スタイルも除く)。保存はしない。"""
    s = re.sub(r"<(script|style)\b.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.S)
    return clean_text(s)


_NORM_DROP = re.compile(r"[\s「」『』\"“”'’・＜＞<>()（）【】\[\]～〜~!！?？\-ー―—:：/／,、。.&＆®™©*＊★☆]+")


def normalize_name(s: str | None) -> str:
    """商品名の照合用: NFKC・実体参照・空白と記号を除き、小文字にする。"""
    s = html_lib.unescape(s or "")
    s = unicodedata.normalize("NFKC", s)
    return _NORM_DROP.sub("", s).lower()


def to_float(v) -> float | None:
    """「401」「21.0」「-」→数値/None。数値はそのまま。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = unicodedata.normalize("NFKC", str(v)).strip().replace(",", "")
    if t in ("", "-", "ー", "−"):
        return None
    m = re.match(r"^-?\d+(?:\.\d+)?", t)
    return float(m.group(0)) if m else None


def parse_yen(text: str | None) -> int | None:
    """「￥1,380」「¥ 560」「680円」→ 整数。見つからなければ None。"""
    t = unicodedata.normalize("NFKC", text or "")
    m = re.search(r"(?:¥|￥)\s*([\d,]+)|([\d,]+)\s*円", t)
    if not m:
        return None
    return int((m.group(1) or m.group(2)).replace(",", ""))


def _round(v: float | None, nd: int) -> float | int | None:
    if v is None:
        return None
    r = round(v, nd)
    return int(r) if nd == 0 else r


def stable_id(prefix: str, name: str) -> str:
    """URLやIDを持たない商品の、名前から作る安定したID(例 sx-1a2b3c4d5e)。"""
    return f"{prefix}-{hashlib.sha1(normalize_name(name).encode('utf-8')).hexdigest()[:10]}"


# ---- 発売日(見出し・本文の「11月2日（月）より」) ------------------------------

_DATE_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")
_LAUNCH_WORD_RE = re.compile(r"より|から|発売|販売|登場|スタート|開始|解禁")


def launch_dates_in_text(text: str | None, ref: datetime.date) -> list[tuple[int, str]]:
    """見出し・本文の中の「M月D日（曜）より/から/発売/販売/登場/スタート」を [(位置, YYYY-MM-DD)] で返す。
    位置は NFKC 化した文字列の中の位置。年は ref の日付から補い、ref から半年以上ずれる年になる場合は前後の年に寄せる。
    「M月D日」の後ろ(20文字以内)に発売の語が続くものだけ採用する(「12月25日まで」「予約は11月1日まで」のような
    終わりの日付を取り違えない)。"""
    t = unicodedata.normalize("NFKC", text or "")
    out = []
    for m in _DATE_RE.finditer(t):
        tail = t[m.end(): m.end() + 20]
        end_pos = re.search(r"まで", tail)
        word = _LAUNCH_WORD_RE.search(tail)
        if not word or (end_pos and end_pos.start() < word.start()):
            continue
        mo, d = int(m.group(1)), int(m.group(2))
        try:
            cand = datetime.date(ref.year, mo, d)
        except ValueError:
            continue
        delta = (cand - ref).days
        if delta > 180:
            cand = datetime.date(ref.year - 1, mo, d)
        elif delta < -180:
            cand = datetime.date(ref.year + 1, mo, d)
        out.append((m.start(), cand.isoformat()))
    return out


def launch_date_from_text(text: str | None, ref: datetime.date) -> str | None:
    """見出し・本文の中の最初の発売日(launch_dates_in_text 参照)。無ければ None。"""
    found = launch_dates_in_text(text, ref)
    return found[0][1] if found else None



# ---------------------------------------------------------------------------
# 月日の補完(年が書かれていない「10/28(水)」の年を決める)
# ---------------------------------------------------------------------------

WEEKDAY_CHARS = "月火水木金土日"


def resolve_month_day(month: int, day: int, ref: datetime.date, weekday: str | None = None) -> str | None:
    """年の無い月日の年を補う。曜日があれば ref の前後1年のうち曜日が合う年、無ければ ref にいちばん近い年。"""
    cands = []
    for y in (ref.year - 1, ref.year, ref.year + 1):
        try:
            cands.append(datetime.date(y, month, day))
        except ValueError:
            continue
    if weekday and weekday[0] in WEEKDAY_CHARS:
        wd = WEEKDAY_CHARS.index(weekday[0])
        hit = [d for d in cands if d.weekday() == wd]
        if hit:
            cands = hit
    if not cands:
        return None
    return min(cands, key=lambda d: abs((d - ref).days)).isoformat()


def _iso_after(month: int, day: int, weekday: str | None, start: datetime.date) -> str | None:
    """開始日 start より後(同日を含む)の、いちばん近い月日の日付。"""
    best = None
    for y in (start.year, start.year + 1):
        try:
            d = datetime.date(y, month, day)
        except ValueError:
            continue
        if d < start:
            continue
        if weekday and weekday[0] in WEEKDAY_CHARS and d.weekday() != WEEKDAY_CHARS.index(weekday[0]):
            continue
        best = d
        break
    return best.isoformat() if best else None


# ---------------------------------------------------------------------------
# スシロー (www.akindo-sushiro.co.jp)
# ---------------------------------------------------------------------------

SUSHIRO_BASE = "https://www.akindo-sushiro.co.jp"
_SUSHIRO_PERIOD = re.compile(
    r"[\[［]\s*(\d{1,2})/(\d{1,2})\s*(?:[（(]([^)）]*)[)）])?\s*[～〜~]\s*"
    r"(?:(\d{1,2})/(\d{1,2})\s*(?:[（(]([^)）]*)[)）])?)?")
_SUSHIRO_QTY = re.compile(r"(販売予定総数|手配総数)\s*([\d.,]+\s*万?\s*[食枚個]分?)")


def _div_inner(block: str, cls: str) -> str | None:
    m = re.search(r'class="' + cls + r'"[^>]*>(.*?)</div>', block, re.S)
    return m.group(1) if m else None


def _kcal_of(text: str | None) -> tuple[float | None, str | None]:
    """「38kcal」「100mlあたり47kcal」→ (kcal, 単位の注記)"""
    t = unicodedata.normalize("NFKC", clean_text(text))
    m = re.search(r"([\d.]+)\s*kcal", t)
    if not m:
        return None, None
    unit = re.search(r"(\d+\s*ml)\s*あたり", t)
    return float(m.group(1)), (unit.group(1).replace(" ", "") + "あたり" if unit else None)


def parse_sushiro_period(annotation: str | None, ref: datetime.date) -> dict | None:
    """商品の注記の先頭「[10/28(水)～11/8(日) 但し販売予定総数343万食が完売次第終了。]」→
    {"start","end","sellout","quantity"}。期間の無い注記は None。年は曜日が合う年に補う。"""
    t = unicodedata.normalize("NFKC", clean_text(annotation))
    m = _SUSHIRO_PERIOD.match(t.strip())
    if not m:
        return None
    sm, sd, swd, em, ed, ewd = m.groups()
    start = resolve_month_day(int(sm), int(sd), ref, swd)
    if not start:
        return None
    end = None
    if em and ed:
        end = _iso_after(int(em), int(ed), ewd, datetime.date.fromisoformat(start))
    head = t[: t.find("]") + 1] if "]" in t else t[:120]
    q = _SUSHIRO_QTY.search(t)
    return {"start": start, "end": end, "sellout": "完売次第" in t,
            "quantity": (q.group(1) + re.sub(r"\s+", "", q.group(2))) if q else None}


def parse_sushiro_menu(html: str, ref: datetime.date) -> dict:
    """店舗別メニュー /menu/menu_detail/?s_id=N -> {"store_name","items":[...],"total"}。

    items は期間の注記([M/D(曜)～…])が付いた品目だけ。品目の形:
      {"id","name","price","kcal","kcal_label","period":{"start","end","sellout","quantity"},"takeout_ng"}
    ページには各品目が2回(PC用・SP用)出るので data-target で重複を除く。品目ブロックが1つも無ければ ValueError。
    説明文・画像は読み取らない。"""
    parts = re.split(r'<div data-target="(\d+)" class="menu-item[^"]*">', html)
    if len(parts) < 3:
        raise ValueError("menu-item が見つかりません(構造変更の疑い)")
    title = re.search(r"<title>\s*(.*?)\s*</title>", html, re.S)
    store = None
    if title:
        m = re.match(r"\s*(.+?)のメニュー", clean_text(title.group(1)))
        store = m.group(1).strip() if m else None
    seen: set[str] = set()
    items = []
    total = 0
    for i in range(1, len(parts), 2):
        iid = parts[i]
        if iid in seen:
            continue
        seen.add(iid)
        block = parts[i + 1].split("<div data-target=")[0]
        name = clean_text(_div_inner(block, "menu-item__name"))
        if not name:
            continue
        total += 1
        price = parse_yen(_div_inner(block, "menu-item__price"))
        ann = _div_inner(block, "menu-item__annotation")
        period = parse_sushiro_period(ann, ref) if ann else None
        if not period:
            continue
        kcal, kcal_label = _kcal_of(_div_inner(block, "menu-item__calorie"))
        items.append({"id": iid, "name": name, "price": price, "kcal": kcal, "kcal_label": kcal_label,
                      "period": period, "takeout_ng": "お持ち帰り対象外" in clean_text(ann)})
    return {"store_name": store, "items": items, "total": total}


def parse_sushiro_news(html: str, base: str = SUSHIRO_BASE) -> list[dict]:
    """/news/ -> [{"date","label","title","url"}]。『商品名』の見出しと公式のお知らせページへのリンクを取る
    (本文は読まない)。"""
    out = []
    for m in re.finditer(r'<a class="commonNews" href="([^"]*)"[^>]*>(.*?)</a>', html, re.S):
        href, inner = m.group(1), m.group(2)
        d = re.search(r'commonNewsDate">\s*(\d{4})\.(\d{2})\.(\d{2})', inner)
        title = re.search(r'commonNews__text">(.*?)</div>', inner, re.S)
        label = re.search(r'c-commonLabel">([^<]*)<', inner)
        if not (d and title):
            continue
        url = href if href.startswith("http") else base + (href if href.startswith("/") else "/" + href)
        out.append({"date": f"{d.group(1)}-{d.group(2)}-{d.group(3)}", "label": clean_text(label.group(1)) if label else "",
                    "title": clean_text(title.group(1)), "url": url})
    return out


# ---------------------------------------------------------------------------
# くら寿司 (www.kurasushi.co.jp)
# ---------------------------------------------------------------------------

KURA_BASE = "https://www.kurasushi.co.jp"
_FAIR_SECTION = re.compile(r"フェア|期間限定|季節限定")


def _image_is_fair(src: str | None) -> bool:
    """商品画像のファイル名に「fair」の区切りがある(例 261030_fair_kohada.jpg)。判定の手がかりとして読むだけ(保存しない)。"""
    if not src:
        return False
    base = src.rsplit("/", 1)[-1]
    return "fair" in re.split(r"[_\-.]", base.lower())


def _image_date(src: str | None) -> str | None:
    """画像ファイル名の先頭の YYMMDD(例 261030_fair_…)を日付にする。読めなければ None。画像名そのものは返さない。"""
    m = re.match(r"(\d{2})(\d{2})(\d{2})_", (src or "").rsplit("/", 1)[-1])
    if not m:
        return None
    try:
        return datetime.date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def parse_kura_menu(html: str) -> dict:
    """/menu/ -> {"items":[...],"price_tier"}。商品ブロック <div id="menu-008831"> を全部読む。

    品目: {"id","name","price","kcal","area","takeout","section","limited","limited_by"}
      limited: 「フェア」「期間限定」「季節限定」の見出しのセクションに属する、またはその商品の画像名に fair の区切りがある。
      limited_by: "section" | "image" | None
    ブロックが1つも無ければ ValueError。説明文・画像は返さない。"""
    t = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    starts = [(m.start(), m.group(1)) for m in re.finditer(r'<div id="menu-(\d+)"', t)]
    if not starts:
        raise ValueError("商品ブロック(menu-NNN)が見つかりません(構造変更の疑い)")
    headings = [(m.start(), clean_text(m.group(1))) for m in
                re.finditer(r'class="menu-section-heading[^"]*"[^>]*>(.*?)</h[2-4]>', t, re.S)]
    tier = re.search(r"\[一皿\s*(\d+)\s*円[〜～~]\s*店舗\]", unicodedata.normalize("NFKC", t))
    items = []
    seen: set[str] = set()
    for idx, (pos, iid) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(t)
        block = t[pos:end]
        if iid in seen:
            continue
        seen.add(iid)
        name = clean_text((re.search(r'class="menu-name"[^>]*>(.*?)</h\d>', block, re.S) or [None, ""])[1])
        if not name:
            continue
        summary = re.search(r'class="menu-summary"[^>]*>(.*?)</ul>', block, re.S)
        ps = [clean_text(x) for x in re.findall(r"<p>(.*?)</p>", summary.group(1) if summary else "", re.S)]
        price = kcal = None
        area = takeout = None
        for j, p in enumerate(ps):
            pn = unicodedata.normalize("NFKC", p)
            if price is None and re.fullmatch(r"[\d,]+円.*", pn):
                price = parse_yen(pn)
            elif kcal is None and re.fullmatch(r"[\d.]+\s*kcal", pn):
                kcal = float(re.match(r"[\d.]+", pn).group(0))
            elif "提供エリア" in p and j + 1 < len(ps):
                area = ps[j + 1]
            elif "お持ち帰り" in p and j + 1 < len(ps):
                takeout = {"可": True, "不可": False}.get(ps[j + 1])
        section = None
        for hp, ht in headings:
            if hp < pos:
                section = ht
        img = re.search(r'class="menu-thumbnail"[^>]*>\s*<img[^>]*\ssrc="([^"]*)"', block)
        by_section = bool(section and _FAIR_SECTION.search(section))
        by_image = _image_is_fair(img.group(1) if img else None)
        items.append({"id": iid, "name": name, "price": price, "kcal": kcal, "area": area, "takeout": takeout,
                      "section": section, "limited": by_section or by_image,
                      "fair_date": _image_date(img.group(1)) if (img and by_image) else None,
                      "limited_by": "section" if by_section else ("image" if by_image else None)})
    return {"items": items, "price_tier": int(tier.group(1)) if tier else None}


# ---------------------------------------------------------------------------
# はま寿司 (www.hamazushi.com)
# ---------------------------------------------------------------------------

HAMA_BASE = "https://www.hamazushi.com"
_HAMA_PRICE_TAX = re.compile(r"税込\s*([\d,]+)\s*円")
_HAMA_PERIOD = re.compile(
    r"(?:(\d{4})/)?(\d{1,2})/(\d{1,2})\s*(?:[（(]([^)）]*)[)）])?\s*[～〜~]\s*(?:(\d{4})/)?(\d{1,2})/(\d{1,2})"
    r"\s*(?:[（(]([^)）]*)[)）])?\s*の期間中")
_HAMA_REGIONS = {
    "hokkaido": "北海道", "touhoku": "東北", "kanto": "関東", "hokuriku": "北陸", "tokai": "東海",
    "kansai": "関西", "chugoku": "中国", "shikoku": "四国", "kyushu": "九州", "okinawa": "沖縄",
}


def _hama_join_name(lines: list[str]) -> str:
    """メニュー名の行(<br>で折り返されている)をつなぐ。「の」「（」の前後は詰め、それ以外は空白1つ。"""
    out = ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if not out:
            out = ln
        elif out.endswith(("の", "と", "・")) or ln.startswith(("（", "(")):
            out += ln
        else:
            out += " " + ln
    return out


def _hama_items(seg: str) -> list[dict]:
    items = []
    for blk in re.split(r'<div class="men-products-list__li"', seg)[1:]:
        text = re.search(r'men-products-item__text">(.*?)</div>', blk, re.S)
        if not text:
            continue
        lines = [clean_text(x) for x in re.split(r"<br\s*/?>", text.group(1))]
        lines = [x for x in lines if x]
        price = None
        name_lines = []
        for ln in lines:
            n = unicodedata.normalize("NFKC", ln)
            m = _HAMA_PRICE_TAX.search(n)
            if m and price is None:
                price = int(m.group(1).replace(",", ""))
                rest = re.sub(r"[\d,]*\s*円?\s*[（(]?\s*税込\s*[\d,]+\s*円\s*[)）]?|税込\s*[\d,]+\s*円", "", ln).strip()
                if rest and not re.fullmatch(r"[\d,\s円]*", rest):
                    name_lines.append(rest)
            else:
                name_lines.append(ln)
        smalls = [clean_text(re.sub(r"<br\s*/?>", " / ", x)) for x in
                  re.findall(r'men-products-item__small">(.*?)</div>', blk, re.S)]
        notes = unicodedata.normalize("NFKC", " ".join(smalls))
        period = None
        pm = _HAMA_PERIOD.search(notes)
        if pm:
            period = {"y1": pm.group(1), "m1": int(pm.group(2)), "d1": int(pm.group(3)), "wd1": pm.group(4),
                      "y2": pm.group(5), "m2": int(pm.group(6)), "d2": int(pm.group(7)), "wd2": pm.group(8)}
        store_notes = [s.strip(" /") for s in re.split(r"\s*/\s*", " / ".join(smalls))
                       if re.search(r"店|北海道|沖縄|県", s) and "お持ち帰り" not in s]
        name = _hama_join_name(name_lines)
        if not name:
            continue
        items.append({"name": name, "price": price, "takeout_ng": "お持ち帰り不可" in notes,
                      "stores_note": "／".join(x.lstrip("※") for x in store_notes)[:100] or None,
                      "period_raw": period})
    return items


def parse_hamazushi_menu(html: str) -> dict:
    """/menu/ -> {"limited":[...],"regional":{地域名:[...]}}。

    期間限定は id="anchor-limited" のセクション、地域限定は id="anchor-limited-hokkaido" 等のセクション。
    HTMLコメントの中の旧商品は読まない。期間限定のセクションが無ければ ValueError(構造変更の疑い)。
    品目: {"name","price"(税込),"takeout_ng","stores_note","period_raw"}"""
    t = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    anchors = [(m.start(), m.group(1)) for m in re.finditer(r'id="(anchor-[a-z-]+)"', t)]
    ids = [a for _, a in anchors]
    if "anchor-limited" not in ids:
        raise ValueError("期間限定のセクション(anchor-limited)が見つかりません(構造変更の疑い)")
    spans = {}
    for k, (pos, aid) in enumerate(anchors):
        end = anchors[k + 1][0] if k + 1 < len(anchors) else len(t)
        spans[aid] = t[pos:end]
    limited = _hama_items(spans["anchor-limited"])
    regional = {}
    for slug, label in _HAMA_REGIONS.items():
        seg = spans.get(f"anchor-limited-{slug}")
        if seg:
            regional[label] = _hama_items(seg)
    return {"limited": limited, "regional": regional}


def hama_period_dates(period_raw: dict | None, ref: datetime.date) -> tuple[str | None, str | None]:
    """「※12/20(土)～2026/1/4(日)の期間中販売」→ (開始日, 終了日)。年が無ければ曜日が合う年に補う。"""
    if not period_raw:
        return None, None
    p = period_raw
    s = resolve_month_day(p["m1"], p["d1"], ref, p.get("wd1"))
    if p.get("y1"):
        try:
            s = datetime.date(int(p["y1"]), p["m1"], p["d1"]).isoformat()
        except ValueError:
            pass
    if not s:
        return None, None
    e = None
    if p.get("y2"):
        try:
            e = datetime.date(int(p["y2"]), p["m2"], p["d2"]).isoformat()
        except ValueError:
            e = None
    else:
        e = _iso_after(p["m2"], p["d2"], p.get("wd2"), datetime.date.fromisoformat(s))
    return s, e


def parse_hama_topic_links(html: str) -> list[str]:
    """トップページ(静的HTML)にあるトピックの個別ページへのパスを新しい順に重複なしで返す。"""
    out: list[str] = []
    for m in re.finditer(r"/topics/\d{4}/\d+\.html", html):
        if m.group(0) not in out:
            out.append(m.group(0))
    return out


def parse_hama_topic(html: str, url: str) -> dict | None:
    """トピックの個別ページ -> {"title","text","date","url"}(発売日の照合専用。text は保存しない)。"""
    t = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.S | re.I)
    plain = clean_text(re.sub(r"<!--.*?-->", " ", t))
    m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})\s*(?:メニュー|お知らせ|店舗情報|キャンペーン|その他)?\s*(.*?)\s*株式会社はま寿司", plain)
    if not m:
        d = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", plain)
        if not d:
            return None
        date = f"{d.group(1)}-{d.group(2)}-{d.group(3)}"
        title = clean_text((re.search(r"<title>(.*?)</title>", html, re.S) or [None, ""])[1]).split("|")[0].strip()
        return {"title": title, "text": plain[d.end():], "date": date, "url": url}
    date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    title = m.group(4).strip()
    return {"title": title, "text": plain[m.end() - len("株式会社はま寿司"):], "date": date, "url": url}


# ---------------------------------------------------------------------------
# かっぱ寿司 (www.kappasushi.jp)
# ---------------------------------------------------------------------------

KAPPA_BASE = "https://www.kappasushi.jp"
KAPPA_LIMITED_CATEGORY = "期間限定ネタ"
KAPPA_FAR_DAYS = 365  # 終了日がこれより先なら「終了予定なし」扱い(定番に 2028-01-12 のような遠い値が入っている)


def _kappa_date(s: str | None) -> str | None:
    m = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", s or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def _kappa_int(v) -> int | None:
    t = unicodedata.normalize("NFKC", str(v if v is not None else "")).strip().replace(",", "")
    return int(t) if re.fullmatch(r"\d+", t) else None


def parse_kappa(menu_text: str, category_text: str, category_name: str = KAPPA_LIMITED_CATEGORY) -> dict:
    """menulist.json と categorylist.json -> {"items":[...], "n_products"}。

    category_name のカテゴリに属する商品を、終了済みのものも含めて返す(終わったかどうかは呼び出し側が日付で決める)。
    品目: {"code","name","price_dine","price_take","kcal","release_date","close_date","dine_only","stores_note"}
      price_dine = sell_price_tax10per(店内の税込)、price_take = sell_price_tax(持ち帰りの税込)。
    構造が想定と違えば ValueError。説明文・画像のキー(comment の説明部分・image_url 等)は読み取らない。"""
    menu = json.loads(menu_text)
    cats = json.loads(category_text)
    products = menu.get("Product") if isinstance(menu, dict) else None
    brand = cats.get("BrandCategory") if isinstance(cats, dict) else None
    if not isinstance(products, list) or not isinstance(brand, list):
        raise ValueError("menulist.json / categorylist.json の構造が想定と違います")
    by_code = {}
    for p in products:
        if not isinstance(p, dict) or not p.get("code") or "name" not in p:
            raise ValueError("menulist.json の商品に code / name がありません")
        by_code[str(p["code"])] = p
    cat = next((c for c in brand if isinstance(c, dict) and c.get("category_name") == category_name), None)
    if cat is None or not isinstance(cat.get("product_lists"), list):
        raise ValueError(f"カテゴリ「{category_name}」が見つかりません")
    items = []
    for code in cat["product_lists"]:
        p = by_code.get(str(code))
        if p is None:
            continue
        name = clean_text(p.get("name"))
        if not name:
            continue
        comment = unicodedata.normalize("NFKC", re.sub(r"<br\s*/?>", "\n", p.get("comment") or ""))
        store_bits = [ln.strip().lstrip("※").strip() for ln in comment.split("\n")
                      if re.search(r"店舗限定|限定販売|のみ販売|取り扱い", ln) and "店内飲食限定" not in ln]
        cal = _kappa_int(p.get("cal"))
        items.append({
            "code": str(code), "name": name,
            "price_dine": _kappa_int(p.get("sell_price_tax10per")), "price_take": _kappa_int(p.get("sell_price_tax")),
            "kcal": float(cal) if cal else None,  # "0" や空は不明
            "release_date": _kappa_date(p.get("release_date")), "close_date": _kappa_date(p.get("close_date")),
            "dine_only": "店内飲食限定" in comment,
            "stores_note": "／".join(store_bits)[:100] or None,
        })
    return {"items": items, "n_products": len(products)}
