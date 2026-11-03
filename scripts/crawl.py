#!/usr/bin/env python3
"""スシロー・くら寿司・はま寿司・かっぱ寿司の期間限定ネタ・フェア商品を巡回し、
data/products/{sushiro|kurasushi|hamazushi|kappasushi}/{id}.json に1商品1ファイルで蓄積する。

このサイトの核は「販売状況の追跡」。公式サイトは販売が終わると商品を消すので、
一覧に居た商品が消えたら終了とみなして記録を残す(カフェポチ・ファストフードポチと同じ設計)。

・既存ファイルは丸ごと上書きしない(初回取得が正)。更新するのは last_seen_at / status /
  price_history 等の追跡用の項目と、まだ空のカロリー・アレルゲン・発売日だけ。
・**初回から、その時点で期間限定・フェアとして載っている商品を全部記録する**。baseline_ids.json は
  「初回に見えた記録対象のID」を持ち、2回目以降の歯止め(新規が異常に大量に出たら分類の誤りや構造変更とみなして保留)
  の判定にだけ使う。
・終了判定は「そのチェーンの一覧取得が全部成功した回」だけ行い、さらに「連続2回(別の日)
  見つからなかった」ときに初めて status=ended にする(かっぱ寿司だけは公式の終了日 close_date も使う)。
・取得失敗はチェーン単位で隔離し(data/pending.json)、他のチェーンへ波及させない。
・ブロック(403・チャレンジ)されたらUA偽装などに切り替えず、止まって報告する。
・4社とも規約が複製・転載を禁じているため、保存するのは事実(商品名・価格・日付・カロリー・
  アレルゲン)だけ。説明文・画像は読み取りもしない。
・価格は店舗で違う。スシローは代表店舗1つ(config の sushiro.s_id)の値、はま寿司・くら寿司は公式の全国/標準の表示。
"""

from __future__ import annotations

import datetime
import json
import re
import sys
import time
import unicodedata
import urllib.robotparser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import parsers  # noqa: E402
import pdf_tables  # noqa: E402
import pdf_update  # noqa: E402
from fetch import BlockedError, NotFoundError, fetch, fetch_binary  # noqa: E402

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA = ROOT / "data"
PRODUCTS = DATA / "products"
PENDING_PATH = DATA / "pending.json"
BASELINE_PATH = DATA / "baseline_ids.json"
RUN_SUMMARY = DATA / "run_summary.json"
LIST_STATE_PATH = DATA / "list_state.json"  # チェーンごとの前回の記録対象の件数(急減の検知用)
PDF_STATE_PATH = DATA / "pdf_state.json"  # 公式PDFの版・指紋(PDFの内容は入れない)
JST = datetime.timezone(datetime.timedelta(hours=9))

UA = CONFIG.get("user_agent", "SushiPochi/1.0 (+https://sushi.netaful.jp/)")
INTERVALS = CONFIG.get("request_interval_seconds", {})
MAX_REQUESTS = CONFIG.get("max_requests_per_run", {})
MIN_INTERVAL = 1.0  # どのチェーンも1秒未満にはしない
# 連続何回(別の日)見つからなければ終了扱いにするか
MISSES_TO_END = 2
# 詳細(栄養・価格)を取り直す間隔(日)と、1回の実行で取り直す最大件数
RECHECK_DAYS = int(CONFIG.get("recheck_days", 7))
RECHECK_MAX = int(CONFIG.get("recheck_max_per_run", 6))
# 記録対象の件数が前回の半分未満になったら、3日続くまで「一覧が壊れた」とみなして終了判定をしない
RATIO_MIN = 0.5
RATIO_MIN_PREV = 8
RATIO_ACCEPT_DAYS = 3


def now_iso() -> str:
    return datetime.datetime.now(JST).isoformat(timespec="seconds")


def today_jst() -> str:
    return datetime.datetime.now(JST).date().isoformat()


# ---------------------------------------------------------------------------
# 保存まわり
# ---------------------------------------------------------------------------

def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, obj, **kw) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1, **kw), encoding="utf-8")


def product_path(chain: str, product_id: str) -> Path:
    return PRODUCTS / chain / f"{product_id}.json"


def save_product(chain: str, product: dict) -> None:
    save_json(product_path(chain, product["product_id"]), product)


def load_product(chain: str, product_id: str) -> dict | None:
    return load_json(product_path(chain, product_id), None)


def load_chain_products(chain: str) -> list[dict]:
    out = []
    d = PRODUCTS / chain
    if d.is_dir():
        for f in sorted(d.glob("*.json")):
            p = load_json(f, None)
            if p:
                out.append(p)
    return out


class Throttle:
    """チェーンごとに最後のリクエスト時刻を覚え、間隔を守って待つ。"""

    def __init__(self):
        self.last: dict[str, float] = {}

    def wait(self, chain: str) -> None:
        interval = max(INTERVALS.get(chain, 2), MIN_INTERVAL)
        last = self.last.get(chain)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < interval:
                time.sleep(interval - elapsed)
        self.last[chain] = time.monotonic()


class Getter:
    """リクエスト数の上限つきの取得器。テストでは差し替える。素のUA(config の user_agent)で取る。"""

    def __init__(self):
        self.throttle = Throttle()
        self.counts: dict[str, int] = {}

    def _tick(self, chain: str) -> None:
        limit = MAX_REQUESTS.get(chain)
        if limit is not None and self.counts.get(chain, 0) >= limit:
            raise RuntimeError(f"{chain}: 1回の実行あたりのリクエスト上限({limit})に達しました")
        self.throttle.wait(chain)
        self.counts[chain] = self.counts.get(chain, 0) + 1

    def __call__(self, chain: str, url: str) -> str:
        self._tick(chain)
        return fetch(url, UA)

    # 公式PDF(アレルゲン・カロリー)用。HEAD(版の確認)とGET(本体)。どちらも同じ間隔・上限・カウントに従う
    def pdf_head(self, chain: str, url: str) -> dict:
        self._tick(chain)
        return fetch_binary(url, UA, method="HEAD")[1]

    def pdf_get(self, chain: str, url: str) -> bytes:
        self._tick(chain)
        return fetch_binary(url, UA)[0]


# ---------------------------------------------------------------------------
# 販売状況の追跡(チェーン共通)
# ---------------------------------------------------------------------------

def record_price(product: dict, price: int | None, price_max: int | None, today: str, relaunch: bool = False) -> bool:
    """価格(と上限)が前回の記録と違えば price_history に追記する。変わったら True。"""
    if price is None:
        return False
    pmax = price_max if (price_max is not None and price_max != price) else None
    hist = product.setdefault("price_history", [])
    last = hist[-1] if hist else None
    if last is not None and last.get("price") == price and last.get("price_max") == pmax:
        return False
    entry = {"date": today, "price": price}
    if pmax is not None:
        entry["price_max"] = pmax
    if relaunch:
        entry["relaunch"] = True
    if last is not None and last.get("date") == today:
        hist[-1] = entry  # 同じ日の再実行は上書き
    else:
        hist.append(entry)
    product["price"] = price
    product["price_max"] = pmax
    return True


def start_new_season(product: dict, old_ended: str | None) -> None:
    """終了後に同じIDが一覧へ戻ってきた(季節商品の再登場)。昨季の期間を seasons に残し、
    今季の発売日は未確定に戻す(昨季の日付を今季として出さない)。"""
    product.setdefault("seasons", []).append({
        "launch_date": product.get("launch_date"),
        "observed_start": season_ref_for_end(product),
        "ended_observed_at": old_ended,
        "news_url": product.get("news_url"),
        "news_title": product.get("news_title"),
    })
    product["launch_date"] = None
    product["launch_date_source"] = None
    for k in ("news_url", "news_title", "news_date"):
        product[k] = None
    product["launch_search_runs"] = 0


def season_ref_for_end(product: dict) -> str:
    """終わった季の観測の起点日(再登場前の relaunches 末尾、無ければ初回に見つけた日)。"""
    rel = product.get("relaunches") or []
    if rel and rel[-1].get("reappeared_at"):
        return rel[-1]["reappeared_at"]
    return (product.get("first_seen_at") or "")[:10]


def touch_seen(product: dict, entry_price: int | None, entry_price_max: int | None, today: str) -> bool:
    """一覧に居た既存商品の追跡項目を更新する。変更があれば True。

    ・last_seen_at を更新、status=on_sale、見失った回数をリセット
    ・終了後に再登場したら on_sale に戻し、relaunches / seasons に記録
    ・一覧の価格が前回と違えば price_history に追記
    """
    relaunched = False
    if product.get("status") == "ended":
        old_ended = product.get("ended_observed_at")
        start_new_season(product, old_ended)
        product.setdefault("relaunches", []).append({
            "ended_observed_at": old_ended,
            "reappeared_at": today,
        })
        product["status"] = "on_sale"
        product["ended_observed_at"] = None
        relaunched = True
    elif product.get("status") != "on_sale":
        product["status"] = "on_sale"
    if product.get("missed_runs"):
        product["missed_runs"] = 0
        product["last_miss_date"] = None
    product["last_seen_at"] = today  # 日付だけ(同じ日に複数回実行してもgitの差分が増えないように)
    record_price(product, entry_price, entry_price_max, today, relaunch=relaunched)
    return relaunched


def apply_missing(chain: str, seen_ids: set[str], today: str, stats: dict) -> None:
    """一覧取得が全部成功した回にだけ呼ぶ。一覧に居なかった販売中の商品を数え、
    連続 MISSES_TO_END 回(別の日)見つからなければ status=ended にする。"""
    for p in load_chain_products(chain):
        if p["product_id"] in seen_ids:
            continue
        if p.get("status") != "on_sale":
            continue
        if p.get("last_miss_date") == today:
            continue  # 同じ日に何度実行しても1回と数える
        p["missed_runs"] = int(p.get("missed_runs") or 0) + 1
        p["last_miss_date"] = today
        if p["missed_runs"] >= MISSES_TO_END:
            p["status"] = "ended"
            p["ended_observed_at"] = today
            stats["ended"] = stats.get("ended", 0) + 1
        save_product(chain, p)


def new_stats() -> dict:
    return {"list_requests": 0, "detail_requests": 0, "new_products": 0, "ended": 0,
            "failed": 0, "blocked": False, "list_ok": False, "limited_seen": 0}


def baseline_for(chain: str) -> set[str] | None:
    data = load_json(BASELINE_PATH, {})
    ids = data.get(chain)
    return set(ids) if ids is not None else None


def save_baseline(chain: str, ids: set[str]) -> None:
    data = load_json(BASELINE_PATH, {})
    data[chain] = sorted(ids)
    save_json(BASELINE_PATH, data)


def ratio_ok(chain: str, n: int, today: str) -> bool:
    """記録対象の件数が前回の半分未満に急減したら、3日続くまで終了判定をしない(取得の不具合の疑い)。
    季節の入れ替わりで本当に減ったなら、3日目に受け入れて基準を更新する。"""
    state = load_json(LIST_STATE_PATH, {})
    cur = state.get(chain) or {}
    prev = int(cur.get("count") or 0)
    if prev >= RATIO_MIN_PREV and n < prev * RATIO_MIN:
        low = dict(cur.get("low") or {})
        if low.get("date") != today:
            low = {"date": today, "days": int(low.get("days") or 0) + 1, "count": n}
        state[chain] = {**cur, "low": low}
        if low["days"] < RATIO_ACCEPT_DAYS:
            save_json(LIST_STATE_PATH, state, sort_keys=True)
            print(f"[WARNING] {chain}: 記録対象が{n}件(前回{prev}件)に急減。{low['days']}日目なので終了判定はしない",
                  file=sys.stderr)
            return False
    state[chain] = {"count": n, "checked_at": today}
    save_json(LIST_STATE_PATH, state, sort_keys=True)
    return True


# ---------------------------------------------------------------------------
# 歯止め: 1回で「新規候補」が極端に多いときは、公式のページ構造・ID体系の変更とみなして記録しない
# ---------------------------------------------------------------------------
# 2026-10-13 に松屋(ファストフードポチ)が商品URLの日付を一斉に更新し、定番メニューまで37件が
# 「新規」になった事故から入れた仕組み。新規候補(baselineにも既存レコードにも無い項目)が
# max(new_burst_min[チェーン別], 一覧の項目数×new_burst_ratio)を超えたら、その回はそのチェーンの
# 新規を記録せず、終了判定もしない。
# **初回(baselineがまだ無い)は対象外**(回転寿司ポチは初回に期間限定を全部記録する)。
# 保留は翌日以降に候補が閾値以下に戻れば自然に再開する。3日連続で保留なら通知が「要判断」に変わる。
# 回転寿司はフェアが2週間前後で入れ替わる(スシロー2026-10-28は一度に10品、くら寿司は約50品が1つのフェア)。
# 通常の入れ替えでは「新規の品」が一覧の大半になるのは普通なので、閾値は「一覧のほぼ全部(95%超)が一度に新規」
# (ID体系の変更を想定)か、「チェーンごとの絶対数(new_burst_abs_by_chain)を超える」(記録対象の判定が崩れて
# 通常メニューまで限定扱いになった桁違いの増え方を想定。くら寿司の画像名の規則が変わる、はま寿司のアンカーが変わる等)
# のどちらか。一覧の全品が一度に入れ替わった日は保留になり、運営者の判断(baselineの更新)が要る。

BURST_KEY = "_suspected_restructure"
BURST_ESCALATE_DAYS = 3


def burst_threshold(chain: str, list_size: int) -> int:
    lo = int((CONFIG.get("new_burst_min_by_chain") or {}).get(chain, CONFIG.get("new_burst_min", 10)))
    ratio = float(CONFIG.get("new_burst_ratio", 0.95))
    limit = max(lo, int(list_size * ratio))
    cap = (CONFIG.get("new_burst_abs_by_chain") or {}).get(chain)
    return min(limit, int(cap)) if cap else limit


def burst_filter(chain: str, items: dict, baseline: set[str] | None, pending: dict, stats: dict,
                 today: str) -> dict:
    """items({商品ID: 一覧の項目})を受け取り、歯止めが働かなければそのまま返す。
    働いたら、既存レコードのある項目だけを返し(最終確認日の更新だけは続ける)、
    stats[chain]["list_ok"]=False(baseline作成・終了判定を止める)と、保留の記録を残す。"""
    st = stats[chain]
    chain_pending = pending.setdefault(chain, {})
    if baseline is None:  # 初回は対象外
        chain_pending.pop(BURST_KEY, None)
        return items
    candidates = [pid for pid in items if pid not in baseline and load_product(chain, pid) is None]
    limit = burst_threshold(chain, len(items))
    if len(candidates) <= limit:
        chain_pending.pop(BURST_KEY, None)
        return items
    prev = chain_pending.get(BURST_KEY) or {}
    if prev.get("date") == today:
        days = int(prev.get("consecutive_days") or 1)
    else:
        days = int(prev.get("consecutive_days") or 0) + 1
    chain_pending[BURST_KEY] = {
        "reason": "suspected_restructure", "count": len(candidates), "threshold": limit,
        "list_size": len(items), "date": today, "consecutive_days": days,
        "first_date": prev.get("first_date") or today,
        "sample": sorted(candidates)[:5],
    }
    st["suspected_restructure"] = {"count": len(candidates), "threshold": limit, "consecutive_days": days}
    st["list_ok"] = False
    msg = (f"[WARNING] {chain}: 新規候補が一度に{len(candidates)}件(閾値{limit}件・一覧{len(items)}件)。"
           "公式のサイト構造変更の疑い。この回は新規を記録せず、終了判定も行わない")
    if days >= BURST_ESCALATE_DAYS:
        msg += f" [{days}日連続で保留中。運営者の判断が必要]"
    print(msg, file=sys.stderr)
    return {pid: e for pid, e in items.items() if load_product(chain, pid) is not None}


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

class Robots:
    """ホストごとに robots.txt を1回だけ取る。404・HTML(robots.txtが無いサイトが返すページ)は全許可。
    取得に失敗したときも全許可として続行する(警告のみ)。403等のブロックは BlockedError。"""

    def __init__(self, getter, chain: str, stats_st: dict):
        self.getter = getter
        self.chain = chain
        self.st = stats_st
        self.parsers: dict[str, urllib.robotparser.RobotFileParser] = {}

    def allowed(self, base: str, path: str) -> bool:
        rp = self.parsers.get(base)
        if rp is None:
            text = None
            try:
                text = self.getter(self.chain, base + "/robots.txt")
                self.st["list_requests"] += 1
            except NotFoundError:
                self.st["list_requests"] += 1
            except BlockedError:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[warn] {self.chain} {base}/robots.txt 取得失敗(全許可として続行): {e}", file=sys.stderr)
            if text and re.search(r"<\s*html", text[:500], re.I):
                text = None  # robots.txtの代わりにHTMLを返すサイト
            rp = urllib.robotparser.RobotFileParser()
            rp.parse((text or "User-agent: *\nAllow: /").splitlines())
            self.parsers[base] = rp
        return rp.can_fetch(UA, base + path)


# ---------------------------------------------------------------------------
# 共通の巡回の流れ
# ---------------------------------------------------------------------------

class Run:
    """1チェーンの1回の巡回の状態と、共通の後処理(記録・追跡・終了判定)。"""

    def __init__(self, chain: str, pending: dict, stats: dict, today: str | None):
        self.chain = chain
        # 日付を指定した実行(テスト)では、初回観測の時刻もその日にそろえる
        self.now = now_iso() if today is None else f"{today}T07:00:00+09:00"
        self.today = today or today_jst()
        self.pending = pending
        self.pending.setdefault(chain, {})
        stats.setdefault(chain, new_stats())
        self.stats = stats
        self.st = stats[chain]

    def block(self, where: str, e: Exception) -> None:
        self.st["blocked"] = True
        print(f"[blocked] {self.chain} {where}: {e}", file=sys.stderr)

    def fail_detail(self, pid: str, url: str, reason: str) -> None:
        self.st["failed"] += 1
        self.pending[self.chain][pid] = {"url": url, "reason": reason, "checked_at": self.now}

    def finish(self, entries: dict[str, dict], list_ok: bool, build, refresh=None, enrich=None,
               present_extra: set[str] | None = None) -> None:
        """entries({商品ID: 一覧の項目})を記録する。

        build(entry) -> 新規レコード(ネットワークに触らない。一覧の情報だけで作る)
        refresh(existing, entry) -> 既存レコードに一覧の最新の項目を反映(純粋な更新。変更があれば True)
        enrich(product, entry, is_new) -> 詳細の取得で栄養などを埋める(ネットワークに触れてよい。変更があれば True)
        present_extra: 記録対象ではないが「公式サイトにまだ載っている」ことを示すID(終了判定に足す)
        """
        chain, st, today = self.chain, self.st, self.today
        st["list_ok"] = bool(list_ok and entries)
        st["limited_seen"] = len(entries)
        if st["list_ok"] and not ratio_ok(chain, len(entries), today):
            st["list_ok"] = False
        baseline = baseline_for(chain)
        entries = burst_filter(chain, entries, baseline, self.pending, self.stats, today)
        for pid, e in entries.items():
            existing = load_product(chain, pid)
            is_new = existing is None
            if is_new:
                product = build(e)
                st["new_products"] += 1
                changed = True
            else:
                product = existing
                relaunched = touch_seen(product, e.get("price"), e.get("price_max"), today)
                if relaunched:
                    st["relaunched"] = st.get("relaunched", 0) + 1
                if refresh:
                    refresh(product, e)
                changed = True
            if enrich is not None and not st["blocked"]:
                try:
                    enrich(product, e, is_new)
                except BlockedError as ex:
                    self.block(f"detail {pid}", ex)
                except NotFoundError as ex:
                    self.fail_detail(pid, e.get("official_url") or "", f"404: {ex}")
                except Exception as ex:  # noqa: BLE001
                    self.fail_detail(pid, e.get("official_url") or "", f"fetch_error: {ex}")
                else:
                    self.pending[chain].pop(pid, None)
            if changed:
                save_product(chain, product)
            if st["blocked"]:
                break
        if st["list_ok"] and not st["blocked"]:
            if baseline is None:
                save_baseline(chain, set(entries))
            apply_missing(chain, set(entries) | (present_extra or set()), today, st)


# ---------------------------------------------------------------------------
# 商品レコードの共通部分
# ---------------------------------------------------------------------------

def base_product(chain: str, pid: str, name: str, now: str, today: str) -> dict:
    """全チェーン共通のレコードの骨格。**説明文・画像は持たない**(規約対策)。"""
    return {
        "chain": chain,
        "product_id": pid,
        "name": name,
        "category": None,
        "price": None,
        "price_max": None,
        "price_note": None,
        "price_variants": None,
        "launch_date": None,
        "launch_date_source": None,
        "news_title": None,
        "news_url": None,
        "news_date": None,
        "listed_at": None,
        "planned_end_text": None,
        "nutrition": None,
        "nutrition_variants": None,
        "nutrition_note": None,
        "nutrition_source": None,
        "allergens": None,
        "allergen_source": None,
        "allergen_note": None,
        "limited_kind": None,
        "stores_note": None,
        "official_url": None,
        "first_seen_at": now,
        "last_seen_at": today,
        "status": "on_sale",
        "ended_observed_at": None,
        "missed_runs": 0,
        "price_history": [],
        "launch_search_runs": 0,
    }


def set_initial_price(product: dict, price: int | None, price_max: int | None, today: str) -> None:
    product["price_history"] = []
    product["price"] = None
    product["price_max"] = None
    record_price(product, price, price_max, today)


def nutrition_kcal(nut: dict | None) -> float | None:
    return (nut or {}).get("kcal")


def apply_nutrition(product: dict, variants: list[dict] | None, rep: dict | None, today: str) -> bool:
    """栄養を入れる。まだ空なら入れ、値が変わっていれば入れ替えて nutrition_changes に残す。変更があれば True。"""
    if not rep:
        return False
    new_nut = rep["nutrition"]
    new_vars = variants if variants and len(variants) > 1 else None
    old = product.get("nutrition")
    if old == new_nut and product.get("nutrition_variants") == new_vars and product.get("nutrition_label") == rep.get("label"):
        return False
    if old is not None and (old != new_nut):
        product.setdefault("nutrition_changes", []).append(
            {"date": today, "from_kcal": nutrition_kcal(old), "to_kcal": nutrition_kcal(new_nut)})
    product["nutrition"] = new_nut
    product["nutrition_variants"] = new_vars
    product["nutrition_label"] = rep.get("label")  # 代表の値がどのサイズ・条件のものか(Tall など)
    return True


def apply_allergens(product: dict, allergens: dict | None, today: str) -> bool:
    if allergens is None or product.get("allergens") == allergens:
        return False
    if product.get("allergens") is not None:
        product.setdefault("allergen_changes", []).append({"date": today})
    product["allergens"] = allergens
    return True


def due_for_recheck(product: dict, today: str) -> bool:
    """詳細を取り直す時期か(RECHECK_DAYS日以上たった、または一度も取れていない)。"""
    last = product.get("detail_checked_at")
    if not last:
        return True
    try:
        return (datetime.date.fromisoformat(today) - datetime.date.fromisoformat(last)).days >= RECHECK_DAYS
    except ValueError:
        return True


# ---- 発売日の照合(見出し・お知らせ・プレスリリース) -----------------------------

LAUNCH_WINDOW_BEFORE_DAYS = 120
LAUNCH_WINDOW_AFTER_DAYS = 14
LAUNCH_SEARCH_RUNS_MAX = 3  # 本文の取得まで使う探索は、新しい商品について最大この回数


def launch_name_keys(name: str) -> list[str]:
    """発売日の照合に使う、商品名の正規化キー(全体と、末尾の（単品）（ホット）・先頭の【店舗限定】を除いたもの)。
    短すぎるキー(4文字以下)は誤爆するので使わない。"""
    keys = []
    variants = [name,
                re.sub(r"[（(][^）)]*[）)]\s*$", "", name),
                re.sub(r"^【[^】]*】", "", name),
                re.sub(r"ミニサイズ", "", re.sub(r"[（(][^）)]*[）)]\s*$", "", name))]
    for v in variants:
        k = parsers.normalize_name(v)
        if len(k) >= 5 and k not in keys:
            keys.append(k)
    return keys


def raw_name_variants(name: str) -> list[str]:
    """本文の中で商品名を探すための、NFKC化した商品名の候補(全体・末尾の括弧なし・先頭の【】なし)。"""
    n = unicodedata.normalize("NFKC", name)
    out = []
    for v in (n, re.sub(r"[(][^)]*[)]\s*$", "", n), re.sub(r"^\[[^\]]*\]", "", re.sub(r"^【[^】]*】", "", n))):
        v = v.strip()
        if len(v) >= 4 and v not in out:
            out.append(v)
    return out


def competitor_keys(name: str, all_names: list[str]) -> list[str]:
    """同じチェーンの他の商品のうち、この商品の名前(正規化)を丸ごと含む、より長い名前のキー。
    「ジョイフルメドレー ティー」が「ジョイフルメドレー ティー ラテ」の記事に誤って結びつくのを防ぐために使う。"""
    mine = launch_name_keys(name)
    out: list[str] = []
    for other in all_names:
        if other == name:
            continue
        for ok in launch_name_keys(other):
            if any(m in ok and m != ok for m in mine) and ok not in out:
                out.append(ok)
    return out


def count_standalone(hay: str, key: str, competitors: list[str]) -> int:
    """hay の中の key の出現数から、より長い別商品の名前の中での出現を引いた数。"""
    n = hay.count(key)
    for c in competitors:
        if key in c:
            n -= hay.count(c) * c.count(key)
    return n


def find_launch(name: str, sources: list[dict], ref: str, all_names: list[str] | None = None) -> dict | None:
    """sources=[{title, text, date, url}] から、商品名が出てくる記事を探し、その発売日を返す。

    date が ref の前 LAUNCH_WINDOW_BEFORE_DAYS 日〜後 LAUNCH_WINDOW_AFTER_DAYS 日の記事だけを対象にする。
    発売日は記事の見出しから、無ければ本文の(商品名の近く→先頭の)「M月D日より」から取る。見つからなければ None。"""
    keys = launch_name_keys(name)
    if not keys or not ref:
        return None
    try:
        ref_d = datetime.date.fromisoformat(ref)
    except ValueError:
        return None
    comp = competitor_keys(name, all_names or [])
    lo = ref_d - datetime.timedelta(days=LAUNCH_WINDOW_BEFORE_DAYS)
    hi = ref_d + datetime.timedelta(days=LAUNCH_WINDOW_AFTER_DAYS)
    for s in sources:
        try:
            d = datetime.date.fromisoformat(s.get("date") or "")
        except ValueError:
            continue
        if not (lo <= d <= hi):
            continue
        title_n = parsers.normalize_name(s.get("title"))
        text = s.get("text") or ""
        text_n = parsers.normalize_name(text)
        in_title = any(count_standalone(title_n, k, comp) > 0 for k in keys)
        in_text = any(count_standalone(text_n, k, comp) > 0 for k in keys)
        if not (in_title or in_text):
            continue
        launch = parsers.launch_date_from_text(s.get("title"), d) if in_title else None
        via = "title"
        if launch is None and text:
            via = "body"
            # 商品名の近くの日付を優先し、無ければ本文の最初の発売日
            text_k = unicodedata.normalize("NFKC", text)
            dates = parsers.launch_dates_in_text(text_k, d)
            for v in raw_name_variants(name):
                pos = text_k.find(v)
                if pos >= 0 and dates:
                    near = min(dates, key=lambda x: abs(x[0] - pos))
                    if abs(near[0] - pos) <= 60:
                        launch = near[1]
                    break
            if launch is None and dates:
                launch = dates[0][1]
        if launch:
            return {"launch_date": launch, "title": s.get("title"), "url": s.get("url"), "date": s.get("date"), "via": via}
    return None


def apply_launch(product: dict, hit: dict, source: str) -> None:
    """発売日を入れる。商品名が見出しに無く本文でだけ見つかった記事は「_body」を付けて区別する
    (1本の記事が複数の商品を扱う・成分名として商品名が出る、などで商品ごとの発売日とずれることがあるため)。"""
    product["launch_date"] = hit["launch_date"]
    product["launch_date_source"] = source + ("_body" if hit.get("via") == "body" else "")
    product["news_title"] = hit["title"]
    product["news_url"] = hit["url"]
    product["news_date"] = hit["date"]


def launch_ref(product: dict) -> str:
    return product.get("listed_at") or (product.get("first_seen_at") or "")[:10]


def needs_launch(product: dict) -> bool:
    return product.get("launch_date") is None and product.get("status") != "ended"




class PressSources:
    """発売日の照合に使う記事の一覧。必要になるまで取らない(遅延取得)。取得に失敗したら空のまま続行。"""

    def __init__(self, loader):
        self.loader = loader
        self.items: list[dict] | None = None

    def get(self) -> list[dict]:
        if self.items is None:
            try:
                self.items = self.loader()
            except BlockedError:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[warn] 発売日の照合元の取得に失敗(発売日なしで続行): {e}", file=sys.stderr)
                self.items = []
        return self.items




# ---------------------------------------------------------------------------
# 4社共通の定数・補助
# ---------------------------------------------------------------------------

PRICE_STORE_NOTE_COMMON = "店舗により価格が異なることがあります"


def pdf_cfg(chain: str) -> dict:
    return (CONFIG.get("pdf") or {}).get(chain) or {}


def source_ref(label: str, url: str | None) -> dict:
    return {"label": label, "url": url}


def set_period(product: dict, start: str | None, end: str | None, sellout: bool = False,
               quantity: str | None = None, end_label: str = "終了予定") -> None:
    """公式の販売期間(メニューの注記・JSONの日付)を事実として残す。"""
    product["period"] = {"start": start, "end": end, "sellout": sellout, "quantity": quantity}
    extra = "・".join(x for x in (("完売次第終了" if sellout else None), quantity) if x)
    if end:
        product["planned_end_text"] = f"{end}まで" + (f"（{extra}）" if extra else "")
    elif extra:
        product["planned_end_text"] = extra
    else:
        product["planned_end_text"] = None


# ---------------------------------------------------------------------------
# スシロー
# ---------------------------------------------------------------------------
# 取得: 店舗別メニュー /menu/menu_detail/?s_id=N(静的HTML。代表店舗は config の sushiro.s_id、既定は巣本店434)。
#   各品目は「国産ほたて貝柱 1貫 110円(税込) 38kcal [10/28(水)～11/8(日) 但し販売予定総数343万食が完売次第終了。]」の形。
#   **[期間]の付いた品目を期間限定として記録する**(記録対象の線引きはこの1点だけ)。
#   価格は代表店舗1つの値(店舗によって110円と140円など違う)。サイト全体と各商品ページにそう明記する。
# お知らせ /news/ は、商品ページに公式のお知らせへのリンクを付けるためだけに読む(1リクエスト。見出しの『商品名』と照合)。
# アレルゲンは公式PDF(pdf_update.py)。カロリーはページにある。

SUSHIRO_MENU_PATH = "/menu/menu_detail/"
SUSHIRO_NEWS_PATH = "/news/"


def sushiro_menu_url() -> str:
    sid = (CONFIG.get("sushiro") or {}).get("s_id", 434)
    return f"{parsers.SUSHIRO_BASE}{SUSHIRO_MENU_PATH}?s_id={sid}"


def crawl_sushiro(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "sushiro"
    run = Run(chain, pending, stats, today)
    st, today, now = run.st, run.today, run.now
    cfg = CONFIG.get("sushiro") or {}
    store_label = cfg.get("store_label") or "巣本店"
    robots = Robots(getter, chain, st)
    menu_url = sushiro_menu_url()
    try:
        if not robots.allowed(parsers.SUSHIRO_BASE, SUSHIRO_MENU_PATH):
            print("[skip] sushiro: robots.txtでDisallow", file=sys.stderr)
            return
        page = getter(chain, menu_url)
        st["list_requests"] += 1
    except BlockedError as e:
        run.block("list", e)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] sushiro メニュー取得失敗: {e}", file=sys.stderr)
        return
    try:
        menu = parsers.parse_sushiro_menu(page, datetime.date.fromisoformat(today))
    except ValueError as e:
        print(f"[warn] sushiro メニューの構造が想定と違う(構造変更の疑い): {e}", file=sys.stderr)
        return
    st["menu_total"] = menu["total"]
    entries: dict[str, dict] = {}
    for it in menu["items"]:
        entries[it["id"]] = {**it, "pid": it["id"], "price_max": None, "official_url": menu_url}

    def news_loader():
        if not robots.allowed(parsers.SUSHIRO_BASE, SUSHIRO_NEWS_PATH):
            return []
        text = getter(chain, parsers.SUSHIRO_BASE + SUSHIRO_NEWS_PATH)
        st["list_requests"] += 1
        return [n for n in parsers.parse_sushiro_news(text) if n["label"] == "メニュー"]

    news = PressSources(news_loader)
    pdf = pdf_cfg(chain)

    def build(e):
        p = base_product(chain, e["pid"], e["name"], now, today)
        p["limited_kind"] = "期間限定"
        p["price_note"] = f"{store_label}の価格です。店舗により異なります"
        set_initial_price(p, e["price"], None, today)
        p["official_url"] = e["official_url"]
        p["launch_date"] = e["period"]["start"]
        p["launch_date_source"] = "menu_period"
        set_period(p, e["period"]["start"], e["period"]["end"], e["period"]["sellout"], e["period"]["quantity"])
        if e.get("takeout_ng"):
            p["stores_note"] = "お持ち帰り対象外"
        if e["kcal"] is not None:
            p["nutrition"] = {"kcal": e["kcal"]}
            p["nutrition_label"] = e["kcal_label"]
            p["nutrition_note"] = "公式サイトで公表されているのはカロリー(kcal)のみです"
        p["nutrition_source"] = source_ref("スシロー 店舗別メニュー", e["official_url"])
        p["allergen_source"] = source_ref(pdf.get("label") or "スシロー アレルギー情報", pdf.get("page") or pdf.get("url"))
        p["source_list"] = [f"akindo-sushiro.co.jp{SUSHIRO_MENU_PATH}?s_id={(CONFIG.get('sushiro') or {}).get('s_id', 434)}"]
        return p

    def refresh(p, e):
        # 期間の注記は延長・変更されることがある(公式の最新の表示を反映)。再登場した季は launch_date が空に戻っている
        pr = e["period"]
        if not p.get("launch_date"):
            p["launch_date"] = pr["start"]
            p["launch_date_source"] = "menu_period"
        set_period(p, pr["start"], pr["end"], pr["sellout"], pr["quantity"])
        if e["kcal"] is not None and not p.get("nutrition"):
            p["nutrition"] = {"kcal": e["kcal"]}
            p["nutrition_label"] = e["kcal_label"]
        elif e["kcal"] is not None and p["nutrition"].get("kcal") != e["kcal"]:
            apply_nutrition(p, None, {"label": e["kcal_label"], "nutrition": {"kcal": e["kcal"]}}, today)

    def enrich(p, e, is_new):
        if p.get("news_url"):
            return
        keys = [k for k in pdf_tables.name_keys(p["name"]) if len(k) >= 5]
        if not keys:
            return
        for n in news.get():
            tn = pdf_tables.norm(n["title"])
            if any(k in tn for k in keys):
                p["news_title"], p["news_url"], p["news_date"] = n["title"], n["url"], n["date"]
                return

    run.finish(entries, True, build, refresh, enrich)


# ---------------------------------------------------------------------------
# くら寿司
# ---------------------------------------------------------------------------
# 取得: /menu/(静的HTML。全223品)。**記録対象は「フェア」の商品**。
#   このHTMLには「〇〇フェア開催中！」の見出しのセクションが無い(にぎり・ぐんかん・サイド・デザートの通常の区分だけ)。
#   フェアの商品を示すのは、商品画像のファイル名の「fair」(例 261030_fair_*。定番は teiban)だけだった(2026-11-03)。
#   なので「フェア/期間限定/季節限定を含む見出しのセクション」を優先し、無ければ画像名の fair を手がかりにする
#   (parse_kura_menu。画像名は判定にだけ使い、保存しない)。ランチ(平日11時〜15時)は期間ではないので対象外。
#   画像名の規則が変わって fair が付かなくなると、記録対象が急減する(ratio_ok が終了判定を止める)。
# 日付: 商品ごとの発売日は無い(launch_date は null、記録開始日を表示)。お知らせにフェアの告知は載らない。
# アレルゲン・カロリー: 公式PDF(カロリーはHTMLにもある)。

KURA_MENU_PATH = "/menu/"


def crawl_kurasushi(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "kurasushi"
    run = Run(chain, pending, stats, today)
    st, today, now = run.st, run.today, run.now
    robots = Robots(getter, chain, st)
    menu_url = parsers.KURA_BASE + KURA_MENU_PATH
    try:
        if not robots.allowed(parsers.KURA_BASE, KURA_MENU_PATH):
            print("[skip] kurasushi: robots.txtでDisallow", file=sys.stderr)
            return
        page = getter(chain, menu_url)
        st["list_requests"] += 1
    except BlockedError as e:
        run.block("list", e)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] kurasushi メニュー取得失敗: {e}", file=sys.stderr)
        return
    try:
        menu = parsers.parse_kura_menu(page)
    except ValueError as e:
        print(f"[warn] kurasushi メニューの構造が想定と違う(構造変更の疑い): {e}", file=sys.stderr)
        return
    st["menu_total"] = len(menu["items"])
    # fair画像は、その回のメニュー改定で出た画像の名残で、古い接頭辞の品は常設化している。
    # 新規に記録するのは、画像名の日付がページ内のfair画像の最新日付から fair_window_days 日以内の品だけ
    # (日付が読めない画像は新規に記録しない)。見出しのセクションの品は常に対象。
    # 記録済みの品は、窓を外れても fair 画像のまま載っている間は販売中として扱う(次のフェアで前のフェア品を終了にしない)
    days = int((CONFIG.get("kurasushi") or {}).get("fair_window_days", 31))
    dates = [it["fair_date"] for it in menu["items"] if it.get("fair_date")]
    latest = datetime.date.fromisoformat(max(dates)) if dates else None
    entries: dict[str, dict] = {}
    for it in menu["items"]:
        if not it["limited"]:
            continue
        in_window = it["limited_by"] == "section" or bool(
            it.get("fair_date") and latest and (latest - datetime.date.fromisoformat(it["fair_date"])).days <= days)
        if not in_window and load_product(chain, it["id"]) is None:
            st["fair_outside_window"] = st.get("fair_outside_window", 0) + 1
            continue
        entries[it["id"]] = {**it, "pid": it["id"], "price_max": None, "official_url": menu_url}
    tier = menu.get("price_tier")
    pdf = pdf_cfg(chain)

    def stores_note(e):
        bits = []
        if e.get("area") and e["area"] != "全店舗":
            bits.append(f"提供エリア：{e['area']}")
        if e.get("takeout") is False:
            bits.append("お持ち帰り不可")
        return "／".join(bits) or None

    def build(e):
        p = base_product(chain, e["pid"], e["name"], now, today)
        p["category"] = e.get("section")
        p["limited_kind"] = "フェア商品"
        p["limited_by"] = e["limited_by"]
        p["price_note"] = (f"［一皿{tier}円〜店舗］の価格です。店舗により異なります" if tier
                           else "店舗により価格が異なります")
        set_initial_price(p, e["price"], None, today)
        p["official_url"] = e["official_url"]
        p["stores_note"] = stores_note(e)
        if e["kcal"] is not None:
            p["nutrition"] = {"kcal": e["kcal"]}
            p["nutrition_note"] = "公式サイトで公表されているのはカロリー(kcal)のみです"
        p["nutrition_source"] = source_ref("くら寿司 メニュー", e["official_url"])
        p["allergen_source"] = source_ref(pdf.get("label") or "くら寿司 アレルゲン・カロリー情報", pdf.get("page") or pdf.get("url"))
        p["source_list"] = ["kurasushi.co.jp/menu/"]
        return p

    def refresh(p, e):
        p["stores_note"] = stores_note(e)
        if e.get("section") and p.get("category") != e["section"]:
            p["category"] = e["section"]
        if e["kcal"] is not None:
            if not p.get("nutrition"):
                p["nutrition"] = {"kcal": e["kcal"]}
            elif p["nutrition"].get("kcal") != e["kcal"]:
                apply_nutrition(p, None, {"label": None, "nutrition": {"kcal": e["kcal"]}}, today)

    run.finish(entries, True, build, refresh, None)


# ---------------------------------------------------------------------------
# はま寿司
# ---------------------------------------------------------------------------
# 取得: /menu/(全国1ページ、静的HTML)。**記録対象は id="anchor-limited"(期間限定)のセクション**。
#   地域限定(anchor-limited-hokkaido 等の10地域)は記録しない: 地域の定番の入れ替えで、期間限定フェアではないため。
#   HTMLコメントの中の旧商品(<!-- ... -->)は読まない。商品IDは無いので名前から作る(sx-…)。
#   価格は「100円（税込110円）」の税込。期間表記は一部の品だけ(「※12/20(土)～2026/1/4(日)の期間中販売」)で、
#   終了日を過ぎた表記が残っていることがある → 終了日が今日より前なら期間として使わない。
# カロリー: 公式サイトの商品ページには無いが、アレルゲン一覧PDF(店内)に「熱量(kcal)(一皿)」の列がある(pdf_update.py が入れる)。
# 発売日: トップページ(静的)にあるトピック(/topics/YYYY/….html、静的)の「11月4日（水）より」を名前で照合する。
#   一覧 /topics/ はJS描画でリンクが取れないので使わない。

HAMA_MENU_PATH = "/menu/"
HAMA_TOPIC_MAX = 3


def crawl_hamazushi(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "hamazushi"
    run = Run(chain, pending, stats, today)
    st, today, now = run.st, run.today, run.now
    robots = Robots(getter, chain, st)
    menu_url = parsers.HAMA_BASE + HAMA_MENU_PATH
    try:
        if not robots.allowed(parsers.HAMA_BASE, HAMA_MENU_PATH):
            print("[skip] hamazushi: robots.txtでDisallow", file=sys.stderr)
            return
        page = getter(chain, menu_url)
        st["list_requests"] += 1
    except BlockedError as e:
        run.block("list", e)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] hamazushi メニュー取得失敗: {e}", file=sys.stderr)
        return
    try:
        menu = parsers.parse_hamazushi_menu(page)
    except ValueError as e:
        print(f"[warn] hamazushi メニューの構造が想定と違う(構造変更の疑い): {e}", file=sys.stderr)
        return
    ref = datetime.date.fromisoformat(today)
    entries: dict[str, dict] = {}
    for it in menu["limited"]:
        pid = parsers.stable_id("hz", it["name"])
        if pid in entries:
            continue
        s, e_ = parsers.hama_period_dates(it["period_raw"], ref)
        stale = bool(e_ and e_ < today)  # 終了日を過ぎた表記が残っている
        entries[pid] = {**it, "pid": pid, "price_max": None, "official_url": menu_url,
                        "start": None if stale else s, "end": None if stale else e_, "period_stale": stale}
    st["regional_skipped"] = sum(len(v) for v in menu["regional"].values())
    all_names = [e["name"] for e in entries.values()]
    pdf = pdf_cfg(chain)

    def topics_loader():
        top = getter(chain, parsers.HAMA_BASE + "/")
        st["list_requests"] += 1
        out = []
        for path in parsers.parse_hama_topic_links(top)[:HAMA_TOPIC_MAX]:
            url = parsers.HAMA_BASE + path
            tp = parsers.parse_hama_topic(getter(chain, url), url)
            st["detail_requests"] += 1
            if tp:
                out.append(tp)
        return out

    press = PressSources(topics_loader)

    def build(e):
        p = base_product(chain, e["pid"], e["name"], now, today)
        p["limited_kind"] = "期間限定"
        p["price_note"] = "全国の価格（税込）です。一部店舗・北海道などでは価格が異なります"
        set_initial_price(p, e["price"], None, today)
        p["official_url"] = e["official_url"]
        notes = []
        if e.get("takeout_ng"):
            notes.append("お持ち帰り不可")
        if e.get("stores_note"):
            notes.append(e["stores_note"])
        p["stores_note"] = "／".join(notes) or None
        if e["start"]:
            p["launch_date"] = e["start"]
            p["launch_date_source"] = "menu_period"
        set_period(p, e["start"], e["end"])
        p["nutrition_source"] = source_ref(pdf.get("label") or "はま寿司 アレルゲン一覧表(店内)", pdf.get("page") or pdf.get("url"))
        p["allergen_source"] = source_ref(pdf.get("label") or "はま寿司 アレルゲン一覧表(店内)", pdf.get("page") or pdf.get("url"))
        p["source_list"] = ["hamazushi.com/menu/#anchor-limited"]
        return p

    def refresh(p, e):
        notes = []
        if e.get("takeout_ng"):
            notes.append("お持ち帰り不可")
        if e.get("stores_note"):
            notes.append(e["stores_note"])
        p["stores_note"] = "／".join(notes) or None
        if e["start"] and not p.get("launch_date"):
            p["launch_date"] = e["start"]
            p["launch_date_source"] = "menu_period"
        if e["start"] or e["end"]:
            set_period(p, e["start"], e["end"])

    def enrich(p, e, is_new):
        if needs_launch(p) and (is_new or int(p.get("launch_search_runs") or 0) < LAUNCH_SEARCH_RUNS_MAX):
            hit = find_launch(p["name"], press.get(), launch_ref(p), all_names)
            if hit:
                apply_launch(p, hit, "official_news")
            else:
                p["launch_search_runs"] = int(p.get("launch_search_runs") or 0) + 1

    run.finish(entries, True, build, refresh, enrich)


# ---------------------------------------------------------------------------
# かっぱ寿司
# ---------------------------------------------------------------------------
# 取得: 公式サイトが読み込む静的JSON /master_data/json/menulist.json(269品。robots.txt は /master_data/ を禁止していない)と
#   categorylist.json(カテゴリ「期間限定ネタ」の商品コード一覧)。
#   **記録対象は「期間限定ネタ」カテゴリの商品のうち、終了日(close_date)が今日以降で、販売開始日(release_date)が今日以前のもの**
#   (JSONには終了済みの商品が残るので、終了日を過ぎた品は新規に記録しない)。
#   記録済みの品がJSONの close_date を過ぎたら、その日を「公式の終了予定日(終了日)」として終了にする
#   (一覧から消えるのを待たない。ended_basis=official_close_date)。
#   定番の close_date に 2028-01-12 のような遠い値が入っている → 1年より先は「終了予定なし」として表示しない。
# 価格: 店内の税込(sell_price_tax10per)を主に、持ち帰りの税込(sell_price_tax)を併記。店内飲食限定の商品は店内のみ。
# カロリー: cal(「0」や空は不明)。アレルゲンは公式PDF(画像でなくテキスト。pdf_update.py)。

KAPPA_MENU_JSON = "/master_data/json/menulist.json"
KAPPA_CATEGORY_JSON = "/master_data/json/categorylist.json"
KAPPA_OFFICIAL_MENU = "https://www.kappasushi.jp/menu2"


def crawl_kappasushi(getter, pending: dict, stats: dict, today: str | None = None) -> None:
    chain = "kappasushi"
    run = Run(chain, pending, stats, today)
    st, today, now = run.st, run.today, run.now
    robots = Robots(getter, chain, st)
    try:
        if not robots.allowed(parsers.KAPPA_BASE, KAPPA_MENU_JSON):
            print("[skip] kappasushi: robots.txtでDisallow", file=sys.stderr)
            return
        menu_text = getter(chain, parsers.KAPPA_BASE + KAPPA_MENU_JSON)
        st["list_requests"] += 1
        cat_text = getter(chain, parsers.KAPPA_BASE + KAPPA_CATEGORY_JSON)
        st["list_requests"] += 1
    except BlockedError as e:
        run.block("list", e)
        return
    except Exception as e:  # noqa: BLE001
        print(f"[warn] kappasushi JSON取得失敗: {e}", file=sys.stderr)
        return
    try:
        data = parsers.parse_kappa(menu_text, cat_text)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"[warn] kappasushi JSONの構造が想定と違う(構造変更の疑い): {e}", file=sys.stderr)
        return
    ref = datetime.date.fromisoformat(today)
    far = (ref + datetime.timedelta(days=parsers.KAPPA_FAR_DAYS)).isoformat()
    entries: dict[str, dict] = {}
    expired: dict[str, str] = {}  # 商品コード -> 公式の終了日(過ぎたもの)
    for it in data["items"]:
        close = it["close_date"]
        if close and close < today:
            expired[it["code"]] = close
            continue
        if it["release_date"] and it["release_date"] > today:
            continue  # まだ販売前(JSONに先に載っている)
        entries[it["code"]] = {**it, "pid": it["code"], "price": it["price_dine"], "price_max": None,
                               "official_url": KAPPA_OFFICIAL_MENU,
                               "close_planned": None if (not close or close > far) else close}
    st["category_total"] = len(data["items"])
    pdf = pdf_cfg(chain)

    def variants(e):
        out = []
        if e["price_dine"] is not None:
            out.append({"label": "店内", "price": e["price_dine"]})
        if e["price_take"] is not None and not e["dine_only"]:
            out.append({"label": "持ち帰り", "price": e["price_take"]})
        return out

    def apply_dates(p, e):
        if e["release_date"] and not p.get("launch_date"):
            p["launch_date"] = e["release_date"]
            p["launch_date_source"] = "official_json"
        set_period(p, e["release_date"], e["close_planned"])

    def build(e):
        p = base_product(chain, e["pid"], e["name"], now, today)
        p["limited_kind"] = "期間限定ネタ"
        p["price_note"] = "店内の税込価格。持ち帰りは税率が異なるため価格が異なります" if not e["dine_only"] \
            else "店内の税込価格です（この商品は店内飲食限定）"
        set_initial_price(p, e["price_dine"], None, today)
        if len(variants(e)) > 1:
            p["price_variants"] = variants(e)
        p["official_url"] = e["official_url"]
        notes = []
        if e["dine_only"]:
            notes.append("店内飲食限定")
        if e.get("stores_note"):
            notes.append(e["stores_note"])
        p["stores_note"] = "／".join(notes) or None
        apply_dates(p, e)
        if e["kcal"] is not None:
            p["nutrition"] = {"kcal": e["kcal"]}
            p["nutrition_note"] = "公式サイトで公表されているのはカロリー(kcal)のみです"
        p["nutrition_source"] = source_ref("かっぱ寿司 メニュー", KAPPA_OFFICIAL_MENU)
        p["allergen_source"] = source_ref(pdf.get("label") or "かっぱ寿司 メニュー別アレルゲン情報", pdf.get("page") or pdf.get("url"))
        p["source_list"] = ["kappasushi.jp/master_data/json/menulist.json"]
        return p

    def refresh(p, e):
        apply_dates(p, e)
        if len(variants(e)) > 1:
            p["price_variants"] = variants(e)
        notes = []
        if e["dine_only"]:
            notes.append("店内飲食限定")
        if e.get("stores_note"):
            notes.append(e["stores_note"])
        p["stores_note"] = "／".join(notes) or None
        if e["kcal"] is not None and (not p.get("nutrition") or p["nutrition"].get("kcal") != e["kcal"]):
            apply_nutrition(p, None, {"label": None, "nutrition": {"kcal": e["kcal"]}}, today)

    run.finish(entries, True, build, refresh, None, present_extra=set(expired))
    # 記録済みで、公式の終了日(close_date)を過ぎた商品は、その日で終了にする(一覧から消えるのを待たない)
    for pid, close in expired.items():
        p = load_product(chain, pid)
        if p is None or p.get("status") != "on_sale":
            continue
        p["status"] = "ended"
        p["ended_observed_at"] = close
        p["ended_basis"] = "official_close_date"
        p["missed_runs"] = 0
        p["last_miss_date"] = None
        set_period(p, (p.get("period") or {}).get("start"), close)
        save_product(chain, p)
        st["ended"] = st.get("ended", 0) + 1


# ---------------------------------------------------------------------------
# 公式PDF(アレルゲン・はま寿司のカロリー)
# ---------------------------------------------------------------------------

def run_pdf_step(chain: str, getter, pending: dict, stats: dict) -> dict | None:
    """公式PDFからアレルゲン(はま寿司はカロリーも)を取り込む(巡回のあとに1回)。

    PDFを読めない(pdfminer未導入・取得失敗・構造変更の疑い)ときも、警告とpending.jsonへの記録だけで
    クロール全体は止めない。取得器がPDF用のメソッドを持たないとき(テスト)・一覧の取得でブロックされた
    ときは何もしない。"""
    st = stats.get(chain)
    if st is None or st.get("blocked") or st.get("error") or not hasattr(getter, "pdf_head"):
        return None
    cfg = pdf_cfg(chain)
    if not cfg.get("url"):
        return None
    state = load_json(PDF_STATE_PATH, {})
    products = load_chain_products(chain)
    try:
        report = pdf_update.update_chain(chain, products, getter, state, pending, cfg, today_jst(), now_iso())
    except Exception as e:  # noqa: BLE001  PDFの失敗でクロール全体を止めない
        print(f"[warn] {chain} のPDF取り込みで例外: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    for p in report["changed_products"]:
        save_product(chain, p)
    save_json(PDF_STATE_PATH, state, sort_keys=True)
    st["pdf"] = {k: report[k] for k in ("head_requests", "get_requests", "matched", "unmatched", "aborted",
                                        "skipped", "blocked")}
    st["pdf"]["changed"] = len(report["changed"])
    return report



# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------

def collect_problems(stats: dict[str, dict]) -> dict[str, list[str]]:
    """この実行で問題があったチェーンを種別ごとに集める(通知用)。

    list_failed は「一覧の取得・解析が完全には成功しなかった」チェーン。取得失敗・商品0件(構造変更の疑い)・
    急減・robots.txtで弾いた一覧など、list_ok が False で終わった経路すべてを含む。
    ただし、ブロック(blocked)・例外(errors)・歯止め(suspected_restructure。既存の「保留」通知がある)は
    重複させないため除く。詳細ページ単発の失敗(stats["failed"])は一時的なことが多く翌日に自動で再試行されるので、
    問題には数えない。"""
    return {
        "blocked": [c for c, s in stats.items() if s.get("blocked")],
        "list_failed": [c for c, s in stats.items()
                        if not s.get("list_ok") and not s.get("blocked") and not s.get("error")
                        and not s.get("suspected_restructure")],
        "errors": [c for c, s in stats.items() if s.get("error")],
    }


def update_health(pending: dict, stats: dict[str, dict], today: str) -> dict[str, int]:
    """チェーンごとの「連続で問題が出ている日数」を pending["_health"] に持ち、今回問題があった
    チェーンの日数を返す。日付で数えるので同じ日の再実行では増えず、1日でも空けば数え直す。
    今回巡回しなかったチェーン(チェーン指定の実行)は触らない。歯止めの保留は別に consecutive_days を持つ。"""
    health = pending.setdefault("_health", {})
    bad = {c for names in collect_problems(stats).values() for c in names}
    yesterday = (datetime.date.fromisoformat(today) - datetime.timedelta(days=1)).isoformat()
    streaks: dict[str, int] = {}
    for chain in stats:
        if chain not in bad:
            health.pop(chain, None)
            continue
        prev = health.get(chain) or {}
        n = int(prev.get("days", 0))
        if prev.get("last_date") != today:
            n = n + 1 if prev.get("last_date") == yesterday else 1
        health[chain] = {"days": n, "last_date": today}
        streaks[chain] = n
    if not health:
        pending.pop("_health", None)
    return streaks


def save_run_summary(stats: dict[str, dict], streaks: dict[str, int] | None = None) -> None:
    """この実行で新規に記録した商品数・終了を観測した商品数・問題があったチェーンを書き出す。
    notify.py が読む。

    日付(UTC/JSTのずれ)で数えると朝の実行で通知が出ない・手動実行で二重に通知が出る
    不具合が姉妹サイト(コンビニポチ)で起きたため、実行ごとに上書きする一時ファイル
    (gitには入れない)にする。

    形式: {"new": {...}, "ended": {...}, "suspected": {...},
           "blocked": [チェーン], "list_failed": [チェーン], "errors": [チェーン],
           "streaks": {チェーン: 連続日数}, "detail_failed": {チェーン: 詳細取得失敗件数}}
    """
    out = {
        "new": {chain: s.get("new_products", 0) for chain, s in stats.items()},
        "ended": {chain: s.get("ended", 0) for chain, s in stats.items()},
        "suspected": {chain: s["suspected_restructure"] for chain, s in stats.items()
                      if s.get("suspected_restructure")},
    }
    problems = collect_problems(stats)
    out.update(problems)
    out["streaks"] = streaks or {}
    listed = set(problems["list_failed"]) | set(problems["blocked"]) | set(problems["errors"])
    out["detail_failed"] = {c: s["failed"] for c, s in stats.items() if s.get("failed") and c not in listed}
    RUN_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    RUN_SUMMARY.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")


CRAWLERS = {"sushiro": crawl_sushiro, "kurasushi": crawl_kurasushi, "hamazushi": crawl_hamazushi,
            "kappasushi": crawl_kappasushi}


def main(argv: list[str] | None = None) -> int:
    # 引数でチェーンを指定すると、そのチェーンだけ巡回する(例: crawl.py sushiro)。
    # 引数なしは全チェーン(Actionsの定期実行)
    argv = sys.argv[1:] if argv is None else argv
    unknown = [a for a in argv if a not in CRAWLERS]
    if unknown:
        print(f"不明なチェーン: {unknown} (指定できるのは {list(CRAWLERS)})", file=sys.stderr)
        return 2
    targets = [CRAWLERS[a] for a in argv] if argv else list(CRAWLERS.values())
    pending = load_json(PENDING_PATH, {})
    stats: dict[str, dict] = {}
    getter = Getter()
    started = time.monotonic()
    names = {v: k for k, v in CRAWLERS.items()}

    for fn in targets:
        try:
            fn(getter, pending, stats)
        except Exception as e:  # noqa: BLE001
            print(f"[error] {fn.__name__} で例外: {e}", file=sys.stderr)
            err_chain = names.get(fn)
            if err_chain:
                stats.setdefault(err_chain, new_stats())["error"] = f"{type(e).__name__}: {e}"
        chain = names.get(fn)
        if chain:
            run_pdf_step(chain, getter, pending, stats)

    streaks = update_health(pending, stats, today_jst())
    save_json(PENDING_PATH, pending, sort_keys=True)
    save_run_summary(stats, streaks)

    print("=== クロール結果 ===")
    for chain, s in stats.items():
        print(
            f"{chain}: 一覧等{s.get('list_requests', 0)}件 / 詳細{s.get('detail_requests', 0)}件 "
            f"(リクエスト計{getter.counts.get(chain, 0)}) / 記録対象として一覧に居た{s.get('limited_seen', 0)}件 "
            f"/ 新規{s.get('new_products', 0)}件 / 終了観測{s.get('ended', 0)}件 / 失敗{s.get('failed', 0)}件 "
            f"/ 一覧取得={'全部成功' if s.get('list_ok') else '一部失敗'} "
            f"/ ブロック={'あり' if s.get('blocked') else 'なし'}"
            + (f" / 例外={s['error']}" if s.get("error") else "")
        )
        if s.get("pdf"):
            print(f"  公式PDF: HEAD{s['pdf']['head_requests']}件・GET{s['pdf']['get_requests']}件 / "
                  f"照合{s['pdf']['matched']}件・更新{s['pdf']['changed']}件 / アレルゲンが入らなかった商品{len(s['pdf']['unmatched'])}件"
                  + (f" / 中止: {s['pdf']['aborted']}" if s['pdf']['aborted'] else "")
                  + (f" / 飛ばした: {s['pdf']['skipped']}" if s['pdf']['skipped'] else ""))
    print(f"所要時間: {time.monotonic() - started:.0f}秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
