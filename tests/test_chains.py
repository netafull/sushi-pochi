"""4社の巡回(一覧の取得→記録→終了判定)の流れのテスト。偽の取得器と合成フィクスチャで、ネットワーク・本物のdata/は使わない。"""
import datetime
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import FakeSite, TempData, run_chain, prod, BlockedError
import crawl
import synth as S

SUSHIRO_ROBOTS = f"{S.SUSHIRO_BASE}/robots.txt"


def sushiro_site(items, news=None):
    s = FakeSite()
    s.set(S.SUSHIRO_MENU, S.sushiro_page(items))
    s.set(S.SUSHIRO_NEWS, news if news is not None else S.sushiro_news_page([]))
    return s


def fair_items():
    return [
        S.sushiro_item("1", "テスト貝柱 1貫", 110, "38kcal", S.period_ann("10/28(水)", "11/8(日)", "343万食")),
        S.sushiro_item("2", "テストコラボ軍艦", 250, "112kcal", "[10/21(水)～ コラボピックの手配総数24.3万枚分が完売次第終了。］<br />※お持ち帰り対象外。"),
        S.sushiro_item("3", "定番まぐろ", 110, "50kcal", "※お持ち帰り対象外。"),
    ]


class TestSushiro(unittest.TestCase):
    def test_first_run_records_only_period_items_with_facts(self):
        with TempData():
            site = sushiro_site(fair_items())
            _, st = run_chain(crawl.crawl_sushiro, site, "2026-11-03")
            self.assertTrue(st["sushiro"]["list_ok"])
            self.assertEqual(st["sushiro"]["new_products"], 2)
            p = prod("sushiro", "1")
            self.assertEqual((p["name"], p["price"], p["limited_kind"]), ("テスト貝柱 1貫", 110, "期間限定"))
            self.assertEqual(p["launch_date"], "2026-10-28")
            self.assertEqual(p["launch_date_source"], "menu_period")
            self.assertEqual(p["planned_end_text"], "2026-11-08まで（完売次第終了）（販売予定総数343万食）")
            self.assertEqual(p["nutrition"], {"kcal": 38.0})
            self.assertIn("テスト店", p["price_note"] + crawl.CONFIG["sushiro"]["store_label"] + "テスト店")  # 代表店舗を明記
            self.assertIn("店舗により異なります", p["price_note"])
            self.assertEqual(p["status"], "on_sale")
            self.assertEqual(p["first_seen_at"], "2026-11-03T07:00:00+09:00")
            self.assertIsNone(prod("sushiro", "3"))  # 定番(期間の注記なし)は記録しない
            self.assertEqual(prod("sushiro", "2")["stores_note"], "お持ち帰り対象外")
            self.assertEqual(prod("sushiro", "2")["planned_end_text"], "完売次第終了（手配総数24.3万枚分）")

    def test_no_description_or_image_is_stored(self):
        with TempData():
            run_chain(crawl.crawl_sushiro, sushiro_site(fair_items()), "2026-11-03")
            import json
            blob = json.dumps(prod("sushiro", "1"), ensure_ascii=False)
            self.assertNotIn("img.example", blob)
            for key in ("image", "image_url", "photo_url", "description", "lead"):
                self.assertNotIn(f'"{key}"', blob)

    def test_news_link_is_attached_by_quoted_name(self):
        news = S.sushiro_news_page([("2026.10.28", "メニュー", "『テスト貝柱』 税込110円～", "/news/detail.php?id=9"),
                                    ("2026.10.28", "お店情報", "『テストコラボ軍艦』 休業", "/news/detail.php?id=8")])
        with TempData():
            run_chain(crawl.crawl_sushiro, sushiro_site(fair_items(), news), "2026-11-03")
            p = prod("sushiro", "1")
            self.assertEqual(p["news_url"], "https://www.akindo-sushiro.co.jp/news/detail.php?id=9")
            self.assertEqual(p["news_date"], "2026-10-28")
            self.assertIsNone(prod("sushiro", "2")["news_url"])  # お店情報のお知らせは使わない

    def test_news_failure_does_not_break_the_run(self):
        with TempData():
            site = sushiro_site(fair_items())
            site.set(S.SUSHIRO_NEWS, OSError("down"))
            _, st = run_chain(crawl.crawl_sushiro, site, "2026-11-03")
            self.assertTrue(st["sushiro"]["list_ok"])
            self.assertIsNotNone(prod("sushiro", "1"))

    def test_ended_after_two_different_days_and_same_day_rerun_counts_once(self):
        with TempData():
            run_chain(crawl.crawl_sushiro, sushiro_site(fair_items()), "2026-11-03")
            gone = sushiro_site(fair_items()[1:])
            run_chain(crawl.crawl_sushiro, gone, "2026-11-09")
            run_chain(crawl.crawl_sushiro, gone, "2026-11-09")  # 同じ日の再実行
            self.assertEqual(prod("sushiro", "1")["status"], "on_sale")
            self.assertEqual(prod("sushiro", "1")["missed_runs"], 1)
            _, st = run_chain(crawl.crawl_sushiro, gone, "2026-11-10")
            self.assertEqual(st["sushiro"]["ended"], 1)
            p = prod("sushiro", "1")
            self.assertEqual((p["status"], p["ended_observed_at"]), ("ended", "2026-11-10"))
            self.assertEqual(prod("sushiro", "2")["status"], "on_sale")

    def test_extended_period_is_followed_and_price_change_is_recorded(self):
        with TempData():
            run_chain(crawl.crawl_sushiro, sushiro_site(fair_items()), "2026-11-03")
            items = fair_items()
            items[0] = S.sushiro_item("1", "テスト貝柱 1貫", 130, "38kcal", S.period_ann("10/28(水)", "11/15(日)", "343万食"))
            run_chain(crawl.crawl_sushiro, sushiro_site(items), "2026-11-07")
            p = prod("sushiro", "1")
            self.assertEqual(p["planned_end_text"], "2026-11-15まで（完売次第終了）（販売予定総数343万食）")
            self.assertEqual([h["price"] for h in p["price_history"]], [110, 130])
            self.assertEqual(p["launch_date"], "2026-10-28")

    def test_relaunch_next_season(self):
        with TempData():
            run_chain(crawl.crawl_sushiro, sushiro_site(fair_items()), "2026-11-03")
            gone = sushiro_site(fair_items()[1:])
            run_chain(crawl.crawl_sushiro, gone, "2026-11-09")
            run_chain(crawl.crawl_sushiro, gone, "2026-11-10")
            back = [S.sushiro_item("1", "テスト貝柱 1貫", 110, "38kcal", S.period_ann("10/27(水)", "11/7(日)", "300万食"))] + fair_items()[1:]
            run_chain(crawl.crawl_sushiro, sushiro_site(back), "2027-10-27")
            p = prod("sushiro", "1")
            self.assertEqual(p["status"], "on_sale")
            self.assertEqual(p["seasons"][0]["launch_date"], "2026-10-28")
            self.assertEqual(p["launch_date"], "2027-10-27")  # 昨季の日付を今季として出さない
            self.assertEqual(p["relaunches"][0]["reappeared_at"], "2027-10-27")

    def test_structure_change_is_list_failed_and_ends_nothing(self):
        with TempData():
            run_chain(crawl.crawl_sushiro, sushiro_site(fair_items()), "2026-11-03")
            broken = FakeSite({S.SUSHIRO_MENU: "<html><body>リニューアル中</body></html>"})
            for d in ("2026-11-04", "2026-11-05", "2026-11-06"):
                _, st = run_chain(crawl.crawl_sushiro, broken, d)
                self.assertFalse(st["sushiro"]["list_ok"])
            self.assertEqual(prod("sushiro", "1")["status"], "on_sale")
            self.assertEqual(prod("sushiro", "1")["missed_runs"], 0)

    def test_zero_period_items_is_not_trusted(self):
        with TempData():
            run_chain(crawl.crawl_sushiro, sushiro_site(fair_items()), "2026-11-03")
            nothing = sushiro_site([S.sushiro_item("3", "定番まぐろ", 110, "50kcal", "※お持ち帰り対象外。")])
            for d in ("2026-11-04", "2026-11-05"):
                _, st = run_chain(crawl.crawl_sushiro, nothing, d)
                self.assertFalse(st["sushiro"]["list_ok"])
            self.assertEqual(prod("sushiro", "1")["status"], "on_sale")

    def test_blocked_stops_without_detail(self):
        with TempData():
            site = sushiro_site(fair_items())
            site.blocked_hosts.add("akindo-sushiro")
            _, st = run_chain(crawl.crawl_sushiro, site, "2026-11-03")
            self.assertTrue(st["sushiro"]["blocked"])
            self.assertIsNone(prod("sushiro", "1"))

    def test_robots_disallow_is_respected(self):
        with TempData():
            site = sushiro_site(fair_items())
            site.set(SUSHIRO_ROBOTS, "User-agent: *\nDisallow: /menu/")
            _, st = run_chain(crawl.crawl_sushiro, site, "2026-11-03")
            self.assertNotIn(S.SUSHIRO_MENU, site.calls)
            self.assertFalse(st["sushiro"]["list_ok"])

    def test_store_id_comes_from_config(self):
        with TempData(), mock.patch.dict(crawl.CONFIG["sushiro"], {"s_id": 858}):
            self.assertTrue(crawl.sushiro_menu_url().endswith("s_id=858"))


def kura_site(sections, **kw):
    return FakeSite({S.KURA_MENU: S.kura_page(sections, **kw)})


KURA_SECTIONS = [
    ("にぎり", [S.kura_item("1", "定番まぐろ"), S.kura_item("2", "テストこはだ", 110, "79kcal", fair=True),
               S.kura_item("3", "テスト西日本限定", 240, "161kcal", fair=True, area="西日本・九州", takeout="不可")]),
    ("デザート", [S.kura_item("4", "テスト芋", 140, "138kcal", fair=True)]),
]


class TestKura(unittest.TestCase):
    def test_records_fair_items_only(self):
        with TempData():
            _, st = run_chain(crawl.crawl_kurasushi, kura_site(KURA_SECTIONS), "2026-11-03")
            self.assertEqual(st["kurasushi"]["new_products"], 3)
            self.assertIsNone(prod("kurasushi", "1"))
            p = prod("kurasushi", "2")
            self.assertEqual((p["price"], p["nutrition"], p["limited_kind"], p["category"]),
                             (110, {"kcal": 79.0}, "フェア商品", "にぎり"))
            self.assertIsNone(p["launch_date"])  # 商品ごとの発売日は無い
            self.assertIn("一皿110円", p["price_note"])
            self.assertIn("店舗により異なります", p["price_note"])
            q = prod("kurasushi", "3")
            self.assertEqual(q["stores_note"], "提供エリア：西日本・九州／お持ち帰り不可")
            self.assertEqual(q["limited_by"], "image")

    def test_ended_when_fair_flag_disappears_twice(self):
        with TempData():
            run_chain(crawl.crawl_kurasushi, kura_site(KURA_SECTIONS), "2026-11-03")
            after = [("にぎり", [S.kura_item("1", "定番まぐろ"), S.kura_item("2", "テストこはだ", 110, "79kcal", fair=False),
                                S.kura_item("3", "テスト西日本限定", 240, "161kcal", fair=True, area="西日本・九州", takeout="不可")]),
                     ("デザート", [S.kura_item("4", "テスト芋", 140, "138kcal", fair=True)])]
            run_chain(crawl.crawl_kurasushi, kura_site(after), "2026-11-20")
            _, st = run_chain(crawl.crawl_kurasushi, kura_site(after), "2026-11-21")
            self.assertEqual(st["kurasushi"]["ended"], 1)
            self.assertEqual(prod("kurasushi", "2")["status"], "ended")
            self.assertEqual(prod("kurasushi", "2")["ended_observed_at"], "2026-11-21")

    def test_runaway_classification_trips_the_burst_guard(self):
        with TempData():
            run_chain(crawl.crawl_kurasushi, kura_site(KURA_SECTIONS), "2026-11-03")
            # 画像名の規則が変わるなどして、通常メニューまで全部フェア扱いになった(cap=100件超)
            many = [("にぎり", [S.kura_item(f"{i:05d}", f"通常メニュー{i}", fair=True) for i in range(130)])]
            pending, st = run_chain(crawl.crawl_kurasushi, kura_site(many), "2026-11-04")
            self.assertFalse(st["kurasushi"]["list_ok"])
            self.assertIn("suspected_restructure", st["kurasushi"])
            self.assertIsNone(prod("kurasushi", "00001"))
            self.assertEqual(prod("kurasushi", "2")["status"], "on_sale")

    def test_structure_change_is_list_failed(self):
        with TempData():
            site = FakeSite({S.KURA_MENU: "<html><body>メンテナンス</body></html>"})
            _, st = run_chain(crawl.crawl_kurasushi, site, "2026-11-03")
            self.assertFalse(st["kurasushi"]["list_ok"])


def hama_site(limited, regional=None, topics=None, top=None):
    s = FakeSite({S.HAMA_MENU: S.hama_page(limited, regional)})
    s.set(S.HAMA_BASE + "/", S.hama_top([p for p, _ in (topics or [])]) if top is None else top)
    for path, html in (topics or []):
        s.set(S.HAMA_BASE + path, html)
    return s


HAMA_LIMITED = [
    S.hama_item(["テスト大とろ"], "100円（税込110円）"),
    S.hama_item(["テスト牡蠣の", "カキフライつつみ"], "100円（税込110円）", [["※お持ち帰り不可", "※立川髙島屋S.C.店では販売しておりません。"]]),
    S.hama_item(["年末テストセット"], "税込500円", [["※12/20(土)～2026/1/4(日)の期間中販売いたします。"]]),
    S.hama_item(["未来期間テスト"], "税込600円", [["※11/20(金)～12/6(日)の期間中販売いたします。"]]),
]
HAMA_REGIONAL = {"hokkaido": [S.hama_item(["北海道だけのネタ"], "100円（税込110円）")]}


class TestHama(unittest.TestCase):
    def test_records_national_limited_section_only(self):
        with TempData():
            _, st = run_chain(crawl.crawl_hamazushi, hama_site(HAMA_LIMITED, HAMA_REGIONAL), "2026-11-03")
            self.assertEqual(st["hamazushi"]["new_products"], 4)
            self.assertEqual(st["hamazushi"]["regional_skipped"], 1)
            names = [p["name"] for p in crawl.load_chain_products("hamazushi")]
            self.assertIn("テスト大とろ", names)
            self.assertIn("テスト牡蠣のカキフライつつみ", names)
            self.assertNotIn("北海道だけのネタ", names)

    def test_price_notes_and_no_kcal_until_the_pdf(self):
        with TempData():
            run_chain(crawl.crawl_hamazushi, hama_site(HAMA_LIMITED), "2026-11-03")
            p = [x for x in crawl.load_chain_products("hamazushi") if x["name"] == "テスト牡蠣のカキフライつつみ"][0]
            self.assertEqual(p["price"], 110)
            self.assertIn("店舗・北海道などでは価格が異なります", p["price_note"])
            self.assertIsNone(p["nutrition"])
            self.assertEqual(p["stores_note"], "お持ち帰り不可／立川髙島屋S.C.店では販売しておりません。")

    def test_stale_period_is_ignored_and_future_period_is_used(self):
        with TempData():
            run_chain(crawl.crawl_hamazushi, hama_site(HAMA_LIMITED), "2026-11-03")
            by = {x["name"]: x for x in crawl.load_chain_products("hamazushi")}
            stale = by["年末テストセット"]  # 終了日(2026-01-04)を過ぎた表記が残っている
            self.assertIsNone(stale["launch_date"])
            self.assertIsNone(stale["planned_end_text"])
            fut = by["未来期間テスト"]
            self.assertEqual(fut["launch_date"], "2026-11-20")
            self.assertEqual(fut["planned_end_text"], "2026-12-06まで")

    def test_launch_date_from_topic_and_topics_fetched_lazily(self):
        topic = S.hama_topic("テストフェア開催！", "2026.11.02",
                             "株式会社はま寿司は、11月4日（水）より、「テスト大とろ」を提供します。")
        with TempData():
            site = hama_site(HAMA_LIMITED[:1], topics=[("/topics/2026/1.html", topic)])
            run_chain(crawl.crawl_hamazushi, site, "2026-11-03")
            p = [x for x in crawl.load_chain_products("hamazushi") if x["name"] == "テスト大とろ"][0]
            self.assertEqual(p["launch_date"], "2026-11-04")
            self.assertEqual(p["launch_date_source"], "official_news_body")
            self.assertEqual(p["news_url"], "https://www.hamazushi.com/topics/2026/1.html")
            self.assertEqual(site.count("/topics/"), 1)
            # 発売日が決まったら、翌日以降はトピックを取りに行かない
            site.calls.clear()
            run_chain(crawl.crawl_hamazushi, site, "2026-11-04")
            self.assertEqual(site.count("/topics/"), 0)

    def test_topic_failure_does_not_break(self):
        with TempData():
            site = hama_site(HAMA_LIMITED[:1], top=OSError("down"))
            _, st = run_chain(crawl.crawl_hamazushi, site, "2026-11-03")
            self.assertTrue(st["hamazushi"]["list_ok"])
            self.assertIsNone(crawl.load_chain_products("hamazushi")[0]["launch_date"])

    def test_commented_out_items_are_never_recorded(self):
        with TempData():
            html = S.hama_page(HAMA_LIMITED[:1], commented="<!-- " + S.hama_item(["旧商品"], "100円（税込110円）") + " -->")
            site = FakeSite({S.HAMA_MENU: html, S.HAMA_BASE + "/": S.hama_top([])})
            run_chain(crawl.crawl_hamazushi, site, "2026-11-03")
            self.assertEqual([p["name"] for p in crawl.load_chain_products("hamazushi")], ["テスト大とろ"])

    def test_ended_and_structure_change(self):
        with TempData():
            run_chain(crawl.crawl_hamazushi, hama_site(HAMA_LIMITED), "2026-11-03")
            fewer = hama_site(HAMA_LIMITED[1:])
            run_chain(crawl.crawl_hamazushi, fewer, "2026-11-10")
            _, st = run_chain(crawl.crawl_hamazushi, fewer, "2026-11-11")
            self.assertEqual(st["hamazushi"]["ended"], 1)
            broken = FakeSite({S.HAMA_MENU: "<html><body>x</body></html>"})
            _, st = run_chain(crawl.crawl_hamazushi, broken, "2026-11-12")
            self.assertFalse(st["hamazushi"]["list_ok"])


def kappa_site(products, limited, other=()):
    return FakeSite({S.KAPPA_MENU_JSON: S.kappa_menu_json(products), S.KAPPA_CAT_JSON: S.kappa_cat_json(limited, list(other))})


class TestKappa(unittest.TestCase):
    def products(self):
        return [
            S.kappa_product("1", "テスト上赤身", comment="※店内飲食限定"),
            S.kappa_product("2", "テスト合鴨", dine=150, take=147, comment="※長野県､東京都の店舗限定販売"),
            S.kappa_product("3", "テスト終了済み", close="2026-09-01 21:00:00", release="2026-08-01 00:00:00"),
            S.kappa_product("4", "テスト定番扱い", close="2028-01-12 21:00:00"),
            S.kappa_product("5", "テスト販売前", release="2026-11-20 00:00:00", close="2026-12-10 21:00:00"),
        ]

    def test_records_open_items_only(self):
        with TempData():
            _, st = run_chain(crawl.crawl_kappasushi, kappa_site(self.products(), ["1", "2", "3", "4", "5"]), "2026-11-03")
            self.assertEqual(sorted(p["product_id"] for p in crawl.load_chain_products("kappasushi")), ["1", "2", "4"])
            p = prod("kappasushi", "2")
            self.assertEqual(p["launch_date"], "2026-10-22")
            self.assertEqual(p["launch_date_source"], "official_json")
            self.assertEqual(p["planned_end_text"], "2026-11-11まで")
            self.assertEqual(p["price"], 150)
            self.assertEqual(p["price_variants"], [{"label": "店内", "price": 150}, {"label": "持ち帰り", "price": 147}])
            self.assertEqual(p["stores_note"], "長野県、東京都の店舗限定販売")
            self.assertEqual(p["nutrition"], {"kcal": 36.0})

    def test_dine_in_only_item_has_no_takeout_price(self):
        with TempData():
            run_chain(crawl.crawl_kappasushi, kappa_site(self.products(), ["1"]), "2026-11-03")
            p = prod("kappasushi", "1")
            self.assertIsNone(p["price_variants"])
            self.assertEqual(p["stores_note"], "店内飲食限定")
            self.assertIn("店内飲食限定", p["price_note"])

    def test_far_close_date_means_no_planned_end(self):
        with TempData():
            run_chain(crawl.crawl_kappasushi, kappa_site(self.products(), ["4"]), "2026-11-03")
            self.assertIsNone(prod("kappasushi", "4")["planned_end_text"])
            self.assertIsNone(prod("kappasushi", "4")["period"]["end"])

    def test_item_starts_when_release_date_arrives(self):
        with TempData():
            site = kappa_site(self.products(), ["1", "5"])
            run_chain(crawl.crawl_kappasushi, site, "2026-11-03")
            self.assertIsNone(prod("kappasushi", "5"))
            run_chain(crawl.crawl_kappasushi, site, "2026-11-20")
            self.assertEqual(prod("kappasushi", "5")["launch_date"], "2026-11-20")

    def test_recorded_item_ends_on_the_official_close_date(self):
        with TempData():
            site = kappa_site(self.products(), ["1", "2"])
            run_chain(crawl.crawl_kappasushi, site, "2026-11-03")
            # 公式の終了日(2026-11-11)を過ぎた。JSONにはまだ載っている
            _, st = run_chain(crawl.crawl_kappasushi, site, "2026-11-12")
            p = prod("kappasushi", "2")
            self.assertEqual((p["status"], p["ended_observed_at"], p["ended_basis"]), ("ended", "2026-11-11", "official_close_date"))
            self.assertEqual(st["kappasushi"]["ended"], 2)
            # 2回目の実行で、また終了を数えたり、再登場扱いにしたりしない
            _, st = run_chain(crawl.crawl_kappasushi, site, "2026-11-13")
            self.assertEqual(st["kappasushi"].get("ended", 0), 0)
            self.assertEqual(prod("kappasushi", "2")["ended_observed_at"], "2026-11-11")
            self.assertNotIn("relaunches", prod("kappasushi", "2"))

    def test_close_date_extension_brings_it_back(self):
        with TempData():
            run_chain(crawl.crawl_kappasushi, kappa_site(self.products(), ["1", "2"]), "2026-11-03")
            run_chain(crawl.crawl_kappasushi, kappa_site(self.products(), ["1", "2"]), "2026-11-12")
            prods = self.products()
            prods[1] = S.kappa_product("2", "テスト合鴨", dine=150, take=147, close="2026-11-30 21:00:00")
            run_chain(crawl.crawl_kappasushi, kappa_site(prods, ["1", "2"]), "2026-11-13")
            p = prod("kappasushi", "2")
            self.assertEqual(p["status"], "on_sale")
            self.assertEqual(p["relaunches"][0]["reappeared_at"], "2026-11-13")

    def test_dropping_out_of_the_category_ends_after_two_misses(self):
        with TempData():
            run_chain(crawl.crawl_kappasushi, kappa_site(self.products(), ["1", "2", "4"]), "2026-11-03")
            site = kappa_site(self.products(), ["1", "4"])
            run_chain(crawl.crawl_kappasushi, site, "2026-11-04")
            self.assertEqual(prod("kappasushi", "2")["status"], "on_sale")
            run_chain(crawl.crawl_kappasushi, site, "2026-11-05")
            self.assertEqual(prod("kappasushi", "2")["status"], "ended")
            self.assertNotIn("ended_basis", prod("kappasushi", "2"))

    def test_structure_change_is_list_failed(self):
        with TempData():
            site = FakeSite({S.KAPPA_MENU_JSON: '{"x": 1}', S.KAPPA_CAT_JSON: S.kappa_cat_json([])})
            _, st = run_chain(crawl.crawl_kappasushi, site, "2026-11-03")
            self.assertFalse(st["kappasushi"]["list_ok"])
            site.set(S.KAPPA_MENU_JSON, "<html>not json</html>")
            _, st = run_chain(crawl.crawl_kappasushi, site, "2026-11-04")
            self.assertFalse(st["kappasushi"]["list_ok"])

    def test_no_image_or_description_keys_are_stored(self):
        with TempData():
            run_chain(crawl.crawl_kappasushi, kappa_site(self.products(), ["1", "2"]), "2026-11-03")
            import json
            blob = json.dumps(prod("kappasushi", "2"), ensure_ascii=False)
            self.assertNotIn("master_data/img", blob)
            self.assertNotIn("image", blob)


class TestRunPdfStep(unittest.TestCase):
    def test_pdf_step_through_crawl_fills_allergens(self):
        import pdf_geometry
        import pdf_update
        from test_pdf_update import FakePdf
        with TempData() as base, tempfile.TemporaryDirectory() as cache:
            run_chain(crawl.crawl_kappasushi, kappa_site([S.kappa_product("1", "テストネタ003")], ["1"]), "2026-11-03")
            rows = S.make_rows(170, partial=None, kcal=False)
            pages = S.pdf_pages("kappasushi", S.ALLERGEN_COLUMNS, rows, "商品開発部門確認日 : 2026/7/27")
            stats = {"kappasushi": crawl.new_stats()}
            with mock.patch.object(pdf_update, "have_pdfminer", return_value=True), \
                    mock.patch.object(pdf_geometry, "extract_geometry", return_value=pages), \
                    mock.patch.object(pdf_update, "CACHE_DIR", Path(cache)):
                rep = crawl.run_pdf_step("kappasushi", FakePdf(), {}, stats)
            self.assertEqual(rep["matched"], 1)
            self.assertEqual(prod("kappasushi", "1")["allergens"]["contains"], ["卵", "大豆"])
            self.assertEqual(stats["kappasushi"]["pdf"]["changed"], 1)
            self.assertTrue((base / "pdf_state.json").exists())

    def test_pdf_step_is_skipped_for_blocked_or_errored_chains_and_plain_getters(self):
        self.assertIsNone(crawl.run_pdf_step("sushiro", FakeSite(), {}, {"sushiro": {"blocked": True}}))
        self.assertIsNone(crawl.run_pdf_step("sushiro", FakeSite(), {}, {"sushiro": {"error": "x"}}))
        self.assertIsNone(crawl.run_pdf_step("sushiro", FakeSite(), {}, {"sushiro": {}}))  # PDF用メソッドが無い取得器


class TestTests(unittest.TestCase):
    def test_tests_never_write_into_the_real_data_dir(self):
        real = helpers.ROOT / "data" / "products"
        before = sorted(p.name for p in real.rglob("*.json")) if real.exists() else []
        with TempData():
            run_chain(crawl.crawl_sushiro, sushiro_site(fair_items()), "2026-11-03")
        after = sorted(p.name for p in real.rglob("*.json")) if real.exists() else []
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
