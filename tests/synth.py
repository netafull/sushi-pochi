"""合成フィクスチャ: 4社の公式データの「構造だけ」を再現した、名前・数値を創作したデータ。

各社の規約が複製・転載を禁じているため、公式のHTML/JSON/PDFをそのままリポジトリに入れない。
ここにあるのはパーサが読む箇所(タグ・クラス名・キー・表の座標)だけを再現した最小のものと、それを組み立てる関数。
商品名・価格・カロリー・アレルゲンの記号はすべて創作(実在の商品とは無関係)。
"""

from __future__ import annotations

import json

# ---------------------------------------------------------------------------
# スシロー
# ---------------------------------------------------------------------------

SUSHIRO_BASE = "https://www.akindo-sushiro.co.jp"
SUSHIRO_MENU = f"{SUSHIRO_BASE}/menu/menu_detail/?s_id=434"
SUSHIRO_NEWS = f"{SUSHIRO_BASE}/news/"


def sushiro_item(iid: str, name: str, price: int = 110, kcal: str | None = "38kcal",
                 annotation: str | None = None) -> str:
    ann = f'<div class="menu-item__annotation">{annotation}</div>' if annotation is not None else ""
    cal = f'<div class="menu-item__calorie">{kcal}</div>' if kcal else '<div class="menu-item__calorie"></div>'
    return f"""<div data-target="{iid}" class="menu-item">
  <img class="menu-item__img" src="https://img.example/menu/{iid}.png" alt="{name}">
  <div class="menu-item__name">{name}</div>
  <div class="menu-item__price">
    {price:,}円(税込)                          </div>
  <div class="menu-item__setdetail"></div>
  {cal}
  {ann}
</div>
"""


def period_ann(start: str = "10/28(水)", end: str | None = "11/8(日)", qty: str = "100万食") -> str:
    tail = f"{end}" if end else ""
    return f"[{start}～{tail}<br />\n但し販売予定総数{qty}が完売次第終了。]"


def sushiro_page(items: list[str], title: str = "テスト店のメニュー | スシローのメニュー | 株式会社テスト") -> str:
    # 本物のページは同じ品目がPC用・SP用で2回出る
    body = "".join(items)
    return f"<html><head><title>{title}</title></head><body>{body}<hr>{body}</body></html>"


def sushiro_news_page(entries: list[tuple[str, str, str, str]]) -> str:
    """entries = [(日付 2026.10.28, ラベル, 見出し, href)]"""
    out = []
    for date, label, title, href in entries:
        out.append(f"""<a class="commonNews" href="{href}" >
  <div class="commonNews__head"><span class="commonNewsDate">{date}</span>
  <span class="commonNewsLabel"><span class="c-commonLabel">{label}</span></span></div>
  <div class="commonNews__text">{title}</div>
</a>""")
    return "<html><body>" + "".join(out) + "</body></html>"


# ---------------------------------------------------------------------------
# くら寿司
# ---------------------------------------------------------------------------

KURA_BASE = "https://www.kurasushi.co.jp"
KURA_MENU = f"{KURA_BASE}/menu/"


def kura_item(iid: str, name: str, price: int = 110, kcal: str | None = "76kcal", fair: bool = False,
              area: str = "全店舗", takeout: str = "可") -> str:
    img = f"261030_{'fair' if fair else 'teiban'}_{iid}.jpg"
    kc = f"<p>{kcal}</p>" if kcal else ""
    return f"""<div id="menu-{iid}" class="
    menu-item
    area1
">
  <div class="menu-thumbnail"><img src="/menu/upload/{img}" alt="{name}" class="lazy" /></div>
  <h4 class="menu-name">{name}</h4>
  <ul class="menu-summary">
    <li><p>{price}円</p>{kc}</li>
    <li><p>【提供エリア】</p><p>{area}</p></li>
    <li><p>【お持ち帰り】</p><p>{takeout}</p></li>
  </ul>
</div>
"""


def kura_page(sections: list[tuple[str, list[str]]], tier: int = 110, commented: str = "") -> str:
    out = [f'<div id="price_{tier}" class="menu-nav section"><h3 class="taxt-ttl">［一皿{tier}円〜店舗］メニューのご案内</h3></div>']
    for k, (heading, items) in enumerate(sections, start=2):
        out.append(f'<section id="menu0{k}" class="menu-section section"><h3 class="menu-section-heading text-serif">'
                   f'{heading}</h3><div class="menu-list">{"".join(items)}</div></section>')
    return f"<html><body>{commented}{''.join(out)}</body></html>"


# ---------------------------------------------------------------------------
# はま寿司
# ---------------------------------------------------------------------------

HAMA_BASE = "https://www.hamazushi.com"
HAMA_MENU = f"{HAMA_BASE}/menu/"


def hama_item(lines: list[str], price_line: str = "100円（税込110円）", smalls: list[str] | None = None) -> str:
    text = "<br>".join(lines + [price_line])
    small = "".join(f'<div class="men-products-item__small">{"<br>".join(s) if isinstance(s, list) else s}</div>'
                    for s in (smalls or [""]))
    return f"""<div class="men-products-list__li">
  <div class="men-products-item">
    <div class="men-products-item__thumb"><img data-src="/assets/menu/img/fair/x.png" alt=""></div>
    <div class="men-products-item__content">
      <div class="men-products-item__text">{text}</div>
      {small}
    </div>
  </div>
</div>
"""


def hama_page(limited: list[str], regional: dict[str, list[str]] | None = None, commented: str = "",
              regular: list[str] | None = None) -> str:
    parts = [f'<a id="anchor-limited" class="u-anchor"></a><h2>期間限定</h2>'
             f'<div class="men-products-list">{commented}{"".join(limited)}</div>']
    parts.append('<a id="anchor-nigiri" class="u-anchor"></a><div class="men-products-list">'
                 + "".join(regular or []) + "</div>")
    for slug, items in (regional or {}).items():
        parts.append(f'<a id="anchor-limited-{slug}" class="u-anchor"></a><div class="men-products-list">'
                      + "".join(items) + "</div>")
    parts.append('<a id="anchor-takeout" class="u-anchor"></a>')
    return "<html><body>" + "".join(parts) + "</body></html>"


def hama_top(paths: list[str]) -> str:
    return "<html><body>" + "".join(f'<a href="{p}">topic</a>' for p in paths) + "</body></html>"


def hama_topic(title: str, date: str, body: str) -> str:
    return (f"<html><head><title>{title} | トピックス | はま寿司</title></head><body><nav>メニュー</nav>"
            f"<h1>{title}</h1><div>{date} メニュー {title} {body}</div></body></html>")


# ---------------------------------------------------------------------------
# かっぱ寿司
# ---------------------------------------------------------------------------

KAPPA_BASE = "https://www.kappasushi.jp"
KAPPA_MENU_JSON = f"{KAPPA_BASE}/master_data/json/menulist.json"
KAPPA_CAT_JSON = f"{KAPPA_BASE}/master_data/json/categorylist.json"


def kappa_product(code: str, name: str, dine: int = 121, take: int = 118, cal: str = "36",
                  release: str = "2026-10-22 00:00:00", close: str = "2026-11-11 21:00:00",
                  comment: str = "") -> dict:
    return {"code": code, "name": name, "name_kana": "x", "sell_price": "110", "sell_price_tax": str(take),
            "sell_price_tax10per": str(dine), "cal": cal, "release_date": release, "close_date": close,
            "comment": comment, "image_url": "/master_data/img/x.png"}


def kappa_menu_json(products: list[dict]) -> str:
    return json.dumps({"Product": products}, ensure_ascii=False)


def kappa_cat_json(limited_codes: list[str], other_codes: list[str] | None = None) -> str:
    return json.dumps({"BrandCategory": [
        {"category_id": "6", "category_name": "期間限定ネタ", "product_lists": list(limited_codes)},
        {"category_id": "5", "category_name": "定番にぎり", "product_lists": list(other_codes or [])},
    ]}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# PDF(座標つきの文字行)
# ---------------------------------------------------------------------------

def pdf_pages(chain_key: str, columns: list[str], rows: list[dict], updated_text: str, per_page: int = 25,
              extra_pages: int = 0, shuffle_second_page: bool = False) -> list[dict]:
    """pdf_geometry.extract_geometry が返す形のページ(texts と chars)を、表の設定(pdf_tables.SPECS)に沿って組み立てる。

    rows = [{"name": 名前, "kcal": 数値|None, "marks": {列名: 記号}}]
    見出しは stacked(1文字ずつ縦に並ぶ)か labels(横書きのラベル)。記号が無い列は、設定の blank_ok なら空欄、
    そうでなければ「無」の記号(はま寿司・かっぱ寿司は - )で埋める。"""
    import pdf_tables as pt
    spec = pt.SPECS[chain_key]
    nx = spec["name_x"][0] + 5.0
    x0 = spec["mark_x0"] + 12.0
    step = 19.0
    none_mark = {"hamazushi": "-", "kappasushi": "-"}.get(chain_key)
    pages = []
    chunks = [rows[i:i + per_page] for i in range(0, max(len(rows), 1), per_page)]
    for pi, chunk in enumerate(chunks):
        texts, chars = [], []

        def add(x, y, s, w=6.0, h=7.0, as_char=True):
            t = [x, y, x + w, y + h, s]
            texts.append(t)
            if as_char:
                for k, ch in enumerate(s):
                    chars.append([x + k * w, y, x + (k + 1) * w, y + h, ch])

        top = 560.0
        add(20, top, updated_text, w=5.0)
        add(300, top, "ページ見出しのテスト行", w=5.0)
        add(nx, 540.0, "※これは注記の行です。表の外にあります。", w=5.0)
        # 見出し
        header_base = 500.0 if spec["header"] == "stacked" else 410.0
        for ci, name in enumerate(columns):
            cxx = x0 + ci * step
            if spec["header"] == "stacked":
                for k, ch in enumerate(name):
                    add(cxx, header_base - k * 8.0, ch, as_char=True)
            else:
                add(cxx - 3.0, header_base, name, w=5.0, as_char=False)
        if spec["drop_digits"]:
            for ci in range(len(columns)):
                add(x0 + ci * step, header_base + 10.0, str(ci + 1), w=3.0)
        add(nx, header_base - 20.0, "商品名", w=5.0)
        # 行
        y = 380.0
        for r in chunk:
            add(nx, y, r["name"], w=5.0, as_char=False)
            if spec["kcal_x"] and r.get("kcal") is not None:
                add(spec["kcal_x"][0] + 3.0, y, str(int(r["kcal"])), w=3.0)
            for ci, cname in enumerate(columns):
                mark = r["marks"].get(cname, none_mark)
                if mark:
                    add(x0 + ci * step, y, mark, w=6.0)
            y -= 13.0
        pages.append({"texts": texts, "chars": chars, "rules": [], "w": 842.0, "h": 595.0})
    for _ in range(extra_pages):
        pages.append({"texts": [[20, 560, 100, 570, "表の無いページ"]], "chars": [], "rules": [], "w": 842.0, "h": 595.0})
    return pages


ALLERGEN_COLUMNS = ["卵", "乳", "小麦", "えび", "かに", "そば", "落花生", "くるみ", "さけ", "大豆"] + \
    [f"品目{chr(0x3042 + i * 2)}" for i in range(14)]


def make_rows(n: int, prefix: str = "テストネタ", kcal: bool = True, partial: str | None = "▲") -> list[dict]:
    """決まった記号の並びを持つ合成の行(名前は prefix + 通し番号)。"""
    rows = []
    for i in range(n):
        marks = {}
        if i % 3 == 0:
            marks["卵"] = "●"
        if i % 4 == 0:
            marks["小麦"] = "●"
        if i % 5 == 0 and partial:
            marks["えび"] = partial
        if i % 2 == 1:
            marks["大豆"] = "●"
        if i % 3 == 1:
            marks["さけ"] = "●"
        rows.append({"name": f"{prefix}{i:03d}", "kcal": (50 + i) if kcal else None, "marks": marks})
    return rows
