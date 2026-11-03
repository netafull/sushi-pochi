#!/usr/bin/env python3
"""回転寿司4社の公式アレルゲン(カロリー)PDFを、商品データに反映する(取得と更新検知の側)。

表の読み取りそのもの(座標つきの文字から表を組み立てる)は pdf_tables.py の純粋関数が行う。
ここは「いつ取得するか」「読み取った結果を、誤った値を出さないよう検証してから商品に入れる」役割。
設計はファストフードポチの pdf_update.py を引き継いでいる。

方針:
  - PDFは毎回は取らない。HEAD(ETag・Last-Modified・Content-Length)を前回の記録と比べ、変わったときだけ
    取得して解析する。アレルゲンが空で、まだこの版で試していない商品があるときも、1回だけ取得して試す
    (同じ版で何度も再試行しない: 商品の pdf_tried に、試した版を覚える)。
    HEADで指紋が取れないPDFは、前回の取得から7日たったときに取り直す。
  - 1回の実行で取得(GET)するPDFは、1社1本まで。HEADは別に1回。
  - 公式PDFの表そのものは保存しない。サイトで記録している商品に名前が一致した行の値(アレルゲン・はま寿司のカロリー)
    だけを商品データに入れる(規約で複製・転載が禁じられているため、PDFの内容の全体を残さない)。
  - 検証ゲート(pdf_tables.validate_table): 見出しが見つからない・行が少ない・列が想定と違う・記号の読み取り率が低い、
    のときは何も更新せず(既存の値を変えない)、エラーを pending.json(と標準エラー)に残して続行する。
    くら寿司は、HTMLのカロリーとPDFのカロリーを突き合わせ、食い違いが多いときも更新しない(列のずれの検出)。
  - 同じ版(更新日)の間は、一度入れた値を上書きしない。
  - 403・429・チャレンジ(BlockedError)になったら、回避せずその回のPDF取得を打ち切る。
  - pdfminer.six が無ければ警告を出してPDFの読み取りだけを飛ばす(クロール全体は止めない)。
"""

from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pdf_tables as pt
from fetch import BlockedError

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".cache" / "pdf"  # gitignore済み。開発中に同じPDFを何度も取らないための置き場(CIでは空)
NO_SIGNATURE_RECHECK_DAYS = 7
KCAL_MISMATCH_MAX = 0.3  # くら寿司: HTMLとPDFのカロリーが食い違う割合の上限


def have_pdfminer() -> bool:
    return importlib.util.find_spec("pdfminer") is not None


def head_signature(headers: dict | None) -> str | None:
    """HEADの応答から、PDFが変わったかを見る指紋。何も取れなければ None。"""
    if not headers:
        return None
    parts = [headers.get(k, "") for k in ("etag", "last-modified", "content-length")]
    return "|".join(parts) if any(parts) else None


def _warn(msg: str) -> None:
    print(f"[pdf] {msg}", file=sys.stderr)


def _record_error(pending: dict, chain: str, url: str, reason: str, now: str) -> None:
    pending.setdefault(chain, {})["pdf:allergen"] = {"url": url, "reason": reason, "checked_at": now}
    _warn(f"{chain}: {reason}")


def _cache_paths(cache_dir: Path, chain: str) -> tuple[Path, Path]:
    return cache_dir / f"{chain}.pdf", cache_dir / f"{chain}.sig"


def load_pdf(fetcher, chain: str, url: str, sig: str | None, cache_dir: Path) -> tuple[bytes, bool]:
    """-> (PDFのバイト列, 実際に取得したか)。キャッシュの指紋が今のHEADと同じなら再取得しない。"""
    pdf_path, sig_path = _cache_paths(cache_dir, chain)
    try:
        if sig and pdf_path.exists() and sig_path.read_text(encoding="utf-8") == sig:
            return pdf_path.read_bytes(), False
    except OSError:
        pass
    data = fetcher.pdf_get(chain, url)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        pdf_path.write_bytes(data)
        sig_path.write_text(sig or "", encoding="utf-8")
    except OSError:
        pass
    return data, True


def _version_text(spec_key: str, version: str) -> str:
    if not version:
        return "更新日不明"
    y, m, d = version.split("-")
    suffix = "現在" if spec_key == "kurasushi" else ("確認" if spec_key == "kappasushi" else "更新")
    return f"{y}年{int(m)}月{int(d)}日{suffix}"


def _needs_attempt(product: dict, token: str, wants_kcal: bool) -> bool:
    incomplete = product.get("allergens") is None or (wants_kcal and product.get("nutrition") is None)
    return incomplete and product.get("pdf_tried") != token


def _kcal_mismatch(products: list[dict], matched: list[tuple[dict, list[dict]]]) -> tuple[int, int]:
    """(HTMLのカロリーと比べられた件数, 食い違った件数)"""
    both = bad = 0
    for p, rows in matched:
        html_kcal = (p.get("nutrition") or {}).get("kcal")
        pdf_kcal = rows[0].get("kcal")
        if html_kcal is None or pdf_kcal is None:
            continue
        both += 1
        if abs(float(html_kcal) - float(pdf_kcal)) > 0.5:
            bad += 1
    return both, bad


def update_chain(chain: str, products: list[dict], fetcher, state: dict, pending: dict, cfg: dict,
                 today: str, now: str, extract=None, cache_dir: Path | None = None) -> dict:
    """PDFのアレルゲン(と、はま寿司ではカロリー)を商品に反映する。変わった商品は products の中身が書き換わる。

    cfg: {"key": SPECS のキー, "url": PDFのURL, "label": 出典の表示名}
    fetcher: pdf_head(chain, url) -> ヘッダー辞書 と pdf_get(chain, url) -> bytes を持つ(crawl.Getter。テストでは偽物)。
    -> {"changed_products", "head_requests", "get_requests", "matched", "unmatched", "blocked", "aborted", "skipped"}
    """
    report = {"changed": [], "head_requests": 0, "get_requests": 0, "matched": 0, "unmatched": [],
              "blocked": False, "aborted": None, "skipped": None, "changed_products": [], "version": None}
    key, url = cfg["key"], cfg["url"]
    spec = pt.SPECS[key]
    wants_kcal = bool(spec["kcal_x"]) and key == "hamazushi"  # カロリーをPDFから取るのははま寿司だけ
    if extract is None:
        if not have_pdfminer():
            report["skipped"] = "pdfminer.six が未インストール"
            _warn(f"{chain}: pdfminer.six が見つからないため、PDFの読み取りを飛ばします(pip install -r requirements-pdf.txt)")
            return report
        from pdf_geometry import extract_geometry
        extract = extract_geometry
    entry = state.setdefault(chain, {})
    token = str(entry.get("version") or "")
    cands = [p for p in products if _needs_attempt(p, token, wants_kcal)]

    # 1) HEADで変更を検知する
    sig = None
    try:
        sig = head_signature(fetcher.pdf_head(chain, url))
        report["head_requests"] += 1
    except BlockedError as e:
        report["blocked"] = True
        _record_error(pending, chain, url, f"blocked: {e}", now)
        return report
    except Exception as e:  # noqa: BLE001
        report["head_requests"] += 1
        _warn(f"{chain}: HEADに失敗(変更は検知できません): {e}")
    if sig is None:
        last = entry.get("last_get")
        due = last is None or (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(last)).days \
            >= NO_SIGNATURE_RECHECK_DAYS
        if not due:
            return report
    else:
        if sig == entry.get("failed_signature"):
            return report  # 前回、構造変更などで読み取れなかったのと同じファイルは取り直さない
        if sig == entry.get("signature") and not cands:
            return report

    # 2) 取得して解析・検証する
    entry["last_get"] = today
    try:
        data, fetched = load_pdf(fetcher, chain, url, sig, cache_dir or CACHE_DIR)
        if fetched:
            report["get_requests"] += 1
    except BlockedError as e:
        report["blocked"] = True
        _record_error(pending, chain, url, f"blocked: {e}", now)
        return report
    except Exception as e:  # noqa: BLE001
        report["get_requests"] += 1
        _record_error(pending, chain, url, f"fetch_error: {e}", now)
        return report
    try:
        pages = extract(data, with_chars=True)
        parsed = pt.parse_table(pages, key)
    except Exception as e:  # noqa: BLE001
        entry["failed_signature"] = sig
        _record_error(pending, chain, url, f"parse_error: {type(e).__name__}: {e}", now)
        return report
    err = pt.validate_table(key, parsed)
    if err:
        entry["failed_signature"] = sig
        _record_error(pending, chain, url, f"validation: {err}", now)
        return report

    # 3) 商品に反映する
    version = parsed["updated"] or ""
    report["version"] = version
    meta = {"url": url, "label": cfg.get("label") or spec["label"], "version": version,
            "version_text": _version_text(key, version), "fetched_at": now}
    matched = []
    for p in products:
        rows = pt.rows_for_name(parsed["rows"], p["name"])
        if rows:
            matched.append((p, rows))
    report["matched"] = len(matched)
    if key == "kurasushi":
        both, bad = _kcal_mismatch(products, matched)
        if both >= 5 and bad / both > KCAL_MISMATCH_MAX:
            report["aborted"] = f"HTMLのカロリーとPDFのカロリーが{both}件中{bad}件食い違うため、更新しません"
            entry["failed_signature"] = sig
            _record_error(pending, chain, url, f"validation: {report['aborted']}", now)
            return report
    changed: dict = {}
    for p, rows in matched:
        row = rows[0]
        src = p.get("allergen_source") or {}
        if p.get("allergens") is not None and src.get("version") == version:
            continue  # 同じ版の間は上書きしない
        new_al = pt.allergens_from_row(key, row)
        if p.get("allergens") is not None and p["allergens"] != new_al:
            p.setdefault("allergen_changes", []).append({"date": today})
        p["allergens"] = new_al
        p["allergen_source"] = dict(meta)
        if wants_kcal and row.get("kcal") is not None:
            old = p.get("nutrition")
            if old is not None and old.get("kcal") != row["kcal"]:
                p.setdefault("nutrition_changes", []).append(
                    {"date": today, "from_kcal": old.get("kcal"), "to_kcal": row["kcal"]})
            p["nutrition"] = {"kcal": row["kcal"]}
            p["nutrition_label"] = "1皿あたり"
            p["nutrition_source"] = dict(meta)
            p["nutrition_note"] = "公式サイトで公表されているのはカロリー(kcal)のみです"
        changed[p["product_id"]] = p
    entry.update(signature=sig, version=version, fetched_at=now)
    entry.pop("failed_signature", None)
    pending.get(chain, {}).pop("pdf:allergen", None)

    # 4) この版で試したことを商品に覚えさせる(同じ版での再試行を避ける)
    for p in products:
        incomplete = p.get("allergens") is None or (wants_kcal and p.get("nutrition") is None)
        if incomplete and p.get("pdf_tried") != version:
            p["pdf_tried"] = version
            changed[p["product_id"]] = p
            if p.get("allergens") is None:
                report["unmatched"].append(p["name"])
    report["changed"] = sorted(changed)
    report["changed_products"] = list(changed.values())
    return report
