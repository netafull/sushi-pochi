"""4社のHTML/JSONパーサのテスト(合成フィクスチャ)。説明文・画像を読み取らないことも確かめる。"""
import datetime
import json
import unittest

import helpers  # noqa: F401
import parsers as P
import synth as S

REF = datetime.date(2026, 11, 3)


class TestDates(unittest.TestCase):
    def test_resolve_month_day_uses_weekday(self):
        self.assertEqual(P.resolve_month_day(10, 28, REF, "水"), "2026-10-28")
        # 曜日が合う年に寄せる(2025年10/28は火曜日、2026年は水曜日)
        self.assertEqual(P.resolve_month_day(10, 28, datetime.date(2026, 1, 5), "水"), "2026-10-28")
        self.assertEqual(P.resolve_month_day(1, 4, datetime.date(2026, 12, 20), "月"), "2027-01-04")

    def test_resolve_month_day_without_weekday_picks_nearest(self):
        self.assertEqual(P.resolve_month_day(12, 20, datetime.date(2026, 1, 5)), "2025-12-20")
        self.assertEqual(P.resolve_month_day(2, 30, REF), None)


class TestSushiro(unittest.TestCase):
    def page(self):
        return S.sushiro_page([
            S.sushiro_item("1", "テスト貝柱 1貫", 110, "38kcal", S.period_ann("10/28(水)", "11/8(日)", "343万食")),
            S.sushiro_item("2", "テストコラボ軍艦", 250, "112kcal",
                           "[10/21(水)～ コラボピックの手配総数24.3万枚分が完売次第終了。］<br />※お持ち帰り対象外。"),
            S.sushiro_item("3", "定番まぐろ", 110, "50kcal", "※お持ち帰り対象外。"),
            S.sushiro_item("4", "テストビール", 590, "100mlあたり47kcal", S.period_ann("11/1(日)", "11/30(月)", "10万食")),
            S.sushiro_item("5", "年またぎネタ", 130, None, S.period_ann("12/20(日)", "1/4(月)", "5万食")),
        ])

    def test_only_items_with_a_period_are_returned_and_duplicates_collapse(self):
        r = P.parse_sushiro_menu(self.page(), REF)
        self.assertEqual(r["store_name"], "テスト店")
        self.assertEqual(r["total"], 5)
        self.assertEqual([i["id"] for i in r["items"]], ["1", "2", "4", "5"])

    def test_period_start_end_sellout_and_quantity(self):
        it = {i["id"]: i for i in P.parse_sushiro_menu(self.page(), REF)["items"]}
        self.assertEqual(it["1"]["period"], {"start": "2026-10-28", "end": "2026-11-08", "sellout": True,
                                             "quantity": "販売予定総数343万食"})
        self.assertEqual(it["2"]["period"]["start"], "2026-10-21")
        self.assertIsNone(it["2"]["period"]["end"])
        self.assertTrue(it["2"]["takeout_ng"])
        self.assertEqual(it["2"]["period"]["quantity"], "手配総数24.3万枚分")
        self.assertEqual(it["5"]["period"]["end"], "2027-01-04")  # 年をまたぐ(曜日が合う年)

    def test_price_and_kcal(self):
        it = {i["id"]: i for i in P.parse_sushiro_menu(self.page(), REF)["items"]}
        self.assertEqual((it["1"]["price"], it["1"]["kcal"], it["1"]["kcal_label"]), (110, 38.0, None))
        self.assertEqual((it["4"]["kcal"], it["4"]["kcal_label"]), (47.0, "100mlあたり"))
        self.assertIsNone(it["5"]["kcal"])

    def test_no_description_or_image_keys(self):
        blob = json.dumps(P.parse_sushiro_menu(self.page(), REF), ensure_ascii=False)
        self.assertNotIn("img.example", blob)
        self.assertNotIn("png", blob)

    def test_structure_change_raises(self):
        with self.assertRaises(ValueError):
            P.parse_sushiro_menu("<html><body>メンテナンス中</body></html>", REF)

    def test_news(self):
        html = S.sushiro_news_page([("2026.10.28", "メニュー", "『テスト貝柱』 税込110円～", "/news/detail.php?id=1"),
                                    ("2026.10.27", "お店情報", "テスト店オープン", "https://x.example/shop/1")])
        n = P.parse_sushiro_news(html)
        self.assertEqual(n[0], {"date": "2026-10-28", "label": "メニュー", "title": "『テスト貝柱』 税込110円～",
                                "url": "https://www.akindo-sushiro.co.jp/news/detail.php?id=1"})
        self.assertEqual(n[1]["url"], "https://x.example/shop/1")


class TestKura(unittest.TestCase):
    def page(self, fair_heading=False):
        return S.kura_page([
            ("にぎり", [S.kura_item("008831", "定番まぐろ", 110, "76kcal"),
                       S.kura_item("008811", "テストこはだ", 110, "79kcal", fair=True),
                       S.kura_item("008814", "テスト西日本限定", 240, "161kcal", fair=True, area="西日本・九州", takeout="不可")]),
            ("サイドメニュー", [S.kura_item("007416", "テスト野菜包み", 130, "35.7kcal", fair=True),
                            S.kura_item("008819", "ランチ　テスト丼【平日11時～15時限定】", 380, "409kcal")]),
            ("かにフェア開催中！" if fair_heading else "デザート",
             [S.kura_item("002939", "テスト芋", 140, "138kcal")]),
        ], commented='<!-- <div id="menu-999999"><h4 class="menu-name">古い商品</h4></div> -->')

    def test_fair_items_by_image_name_when_there_is_no_fair_section(self):
        r = P.parse_kura_menu(self.page())
        lim = {i["id"]: i for i in r["items"] if i["limited"]}
        self.assertEqual(sorted(lim), ["007416", "008811", "008814"])
        self.assertTrue(all(i["limited_by"] == "image" for i in lim.values()))
        self.assertEqual(len(r["items"]), 6)  # コメントの中の商品は読まない
        self.assertEqual(r["price_tier"], 110)

    def test_fair_section_heading_is_preferred_and_covers_every_item_in_it(self):
        r = P.parse_kura_menu(self.page(fair_heading=True))
        it = {i["id"]: i for i in r["items"]}
        self.assertTrue(it["002939"]["limited"])
        self.assertEqual(it["002939"]["limited_by"], "section")

    def test_fields(self):
        it = {i["id"]: i for i in P.parse_kura_menu(self.page())["items"]}
        self.assertEqual((it["007416"]["price"], it["007416"]["kcal"]), (130, 35.7))
        self.assertEqual((it["008814"]["area"], it["008814"]["takeout"]), ("西日本・九州", False))
        self.assertEqual(it["008811"]["section"], "にぎり")
        self.assertTrue(it["008811"]["takeout"])

    def test_fair_date_comes_from_the_image_prefix(self):
        h = S.kura_page([("にぎり", [S.kura_item("1", "a", fair=True, date="260626"), S.kura_item("2", "b", fair=True, date="zzzzzz"),
                                   S.kura_item("3", "c", fair=False)])])
        it = {i["id"]: i for i in P.parse_kura_menu(h)["items"]}
        self.assertEqual(it["1"]["fair_date"], "2026-06-26")
        self.assertIsNone(it["2"]["fair_date"])
        self.assertIsNone(it["3"]["fair_date"])

    def test_image_name_is_not_kept(self):
        blob = json.dumps(P.parse_kura_menu(self.page()), ensure_ascii=False)
        self.assertNotIn("261030", blob)
        self.assertNotIn(".jpg", blob)

    def test_structure_change_raises(self):
        with self.assertRaises(ValueError):
            P.parse_kura_menu("<html><body><div class='menu'>x</div></body></html>")


class TestHama(unittest.TestCase):
    def page(self):
        limited = [
            S.hama_item(["テスト大とろ"], "100円（税込110円）"),
            S.hama_item(["広島県産牡蠣の", "カキフライつつみ", "（タルタルソース）"], "100円（税込110円）",
                        [["※お持ち帰り不可", "※立川髙島屋S.C.店では販売しておりません。"]]),
            S.hama_item(["アカイカ", "柚子おろし盛り"], "160円（税込176円）"),
            S.hama_item(["期間表記つき"], "税込500円", [["※12/20(土)～2026/1/4(日)の期間中販売いたします。"]]),
        ]
        regional = {"hokkaido": [S.hama_item(["北海道だけのネタ"], "100円（税込110円）")]}
        commented = '<!-- ' + S.hama_item(["コメントの中の旧商品"], "100円（税込110円）") + ' -->'
        return S.hama_page(limited, regional, commented, regular=[S.hama_item(["定番ネタ"], "100円（税込110円）")])

    def test_only_the_limited_section_is_returned(self):
        r = P.parse_hamazushi_menu(self.page())
        self.assertEqual([i["name"] for i in r["limited"]],
                         ["テスト大とろ", "広島県産牡蠣のカキフライつつみ（タルタルソース）", "アカイカ 柚子おろし盛り", "期間表記つき"])
        self.assertNotIn("定番ネタ", json.dumps(r, ensure_ascii=False))
        self.assertNotIn("コメントの中の旧商品", json.dumps(r, ensure_ascii=False))

    def test_regional_items_are_parsed_but_kept_apart(self):
        r = P.parse_hamazushi_menu(self.page())
        self.assertEqual([i["name"] for i in r["regional"]["北海道"]], ["北海道だけのネタ"])

    def test_price_is_the_tax_included_one(self):
        r = P.parse_hamazushi_menu(self.page())
        self.assertEqual([i["price"] for i in r["limited"]], [110, 110, 176, 500])

    def test_notes(self):
        it = P.parse_hamazushi_menu(self.page())["limited"][1]
        self.assertTrue(it["takeout_ng"])
        self.assertEqual(it["stores_note"], "立川髙島屋S.C.店では販売しておりません。")

    def test_period_dates_and_stale_period(self):
        it = P.parse_hamazushi_menu(self.page())["limited"][3]
        self.assertEqual(P.hama_period_dates(it["period_raw"], datetime.date(2026, 11, 3)), ("2025-12-20", "2026-01-04"))  # 年末年始の表記が残っている(曜日の合う年・終了日は明記の年)
        self.assertEqual(P.hama_period_dates(None, REF), (None, None))

    def test_missing_limited_section_raises(self):
        with self.assertRaises(ValueError):
            P.parse_hamazushi_menu('<html><a id="anchor-nigiri"></a></html>')

    def test_topics(self):
        self.assertEqual(P.parse_hama_topic_links(S.hama_top(["/topics/2026/1102000876.html", "/topics/2026/1008000866.html",
                                                              "/topics/2026/1102000876.html"])),
                         ["/topics/2026/1102000876.html", "/topics/2026/1008000866.html"])
        t = P.parse_hama_topic(S.hama_topic("テストフェア開催！", "2026.11.02", "株式会社はま寿司が11月4日（水）より開催します。"), "u")
        self.assertEqual((t["title"], t["date"]), ("テストフェア開催！", "2026-11-02"))
        self.assertIn("11月4日", t["text"])


class TestKappa(unittest.TestCase):
    def data(self):
        prods = [
            S.kappa_product("1", "テスト上赤身<br>～ミナミ～", comment="※店内飲食限定"),
            S.kappa_product("2", "テスト合鴨", comment="※長野県､東京都の店舗限定販売", dine=150, take=147),
            S.kappa_product("3", "テスト終了済み", close="2026-09-01 21:00:00"),
            S.kappa_product("4", "テスト定番扱い", close="2028-01-12 21:00:00", cal="0"),
        ]
        return S.kappa_menu_json(prods), S.kappa_cat_json(["1", "2", "3", "4"], ["5"])

    def test_category_members_are_returned_including_closed(self):
        r = P.parse_kappa(*self.data())
        self.assertEqual([i["code"] for i in r["items"]], ["1", "2", "3", "4"])
        self.assertEqual(r["n_products"], 4)

    def test_fields(self):
        it = {i["code"]: i for i in P.parse_kappa(*self.data())["items"]}
        self.assertEqual(it["1"]["name"], "テスト上赤身 ～ミナミ～")
        self.assertEqual((it["1"]["price_dine"], it["1"]["price_take"], it["1"]["kcal"]), (121, 118, 36.0))
        self.assertTrue(it["1"]["dine_only"])
        self.assertEqual(it["1"]["release_date"], "2026-10-22")
        self.assertEqual(it["1"]["close_date"], "2026-11-11")
        self.assertEqual(it["2"]["stores_note"], "長野県、東京都の店舗限定販売")
        self.assertFalse(it["2"]["dine_only"])
        self.assertIsNone(it["4"]["kcal"])  # "0" は不明

    def test_image_and_description_keys_are_not_returned(self):
        blob = json.dumps(P.parse_kappa(*self.data()), ensure_ascii=False)
        self.assertNotIn("image", blob)
        self.assertNotIn("master_data", blob)

    def test_structure_change_raises(self):
        with self.assertRaises(ValueError):
            P.parse_kappa(json.dumps({"x": 1}), S.kappa_cat_json([]))
        with self.assertRaises(ValueError):
            P.parse_kappa(S.kappa_menu_json([{"name": "コード無し"}]), S.kappa_cat_json([]))
        with self.assertRaises(ValueError):
            P.parse_kappa(S.kappa_menu_json([]), json.dumps({"BrandCategory": [{"category_name": "別のカテゴリ", "product_lists": []}]}))


if __name__ == "__main__":
    unittest.main()
