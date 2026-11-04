import json
import re
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401
import generate_site as g


def item(**kw):
    base = {
        "chain": "sushiro", "product_id": "9001", "name": "テストラテ", "category": "ビバレッジ",
        "price": 600, "price_max": 700, "price_note": "サイズにより異なります", "price_variants": None,
        "launch_date": "2026-09-02", "launch_date_source": "official_news",
        "news_title": "テストの見出し", "news_url": "https://news.example/p1", "news_date": "2026-08-30",
        "planned_end_text": None, "listed_at": None,
        "nutrition": {"kcal": 246, "protein_g": 5.9, "fat_g": 14.0, "carbs_g": 24.5, "sugar_g": 23.8,
                      "fiber_g": 0.7, "salt_g": 0.41, "other": {"飽和脂肪酸": "7.9g"}},
        "nutrition_label": "Tall", "nutrition_variants": None, "nutrition_note": "公式の値です",
        "nutrition_source": {"label": "テスト栄養ページ", "url": "https://nutrition.example/"},
        "allergens": {"contains": ["乳", "小麦"], "partial": [], "partial_meaning": "同じラインで製造しています"},
        "allergen_source": {"label": "テストアレルゲンページ", "url": "https://allergen.example/", "version_text": "2026年10月22日更新"},
        "allergen_note": None,
        "limited_kind": "期間限定", "stores_note": "お持ち帰り対象外",
        "official_url": "https://menu.sushiro.example/9001",
        "first_seen_at": "2026-11-03T07:00:00+09:00", "last_seen_at": "2026-11-03",
        "status": "on_sale", "ended_observed_at": None,
        "price_history": [{"date": "2026-11-03", "price": 600, "price_max": 700}],
    }
    base.update(kw)
    return base


class TestSeo(unittest.TestCase):
    def test_title_with_kcal(self):
        self.assertEqual(g.seo_title(item()), "スシロー「テストラテ」のカロリー・価格・アレルゲン")

    def test_title_without_nutrition(self):
        self.assertEqual(g.seo_title(item(nutrition=None, chain="hamazushi", name="新作")),
                         "はま寿司「新作」の価格・アレルゲン・販売期間")

    def test_description_has_chain_kcal_price(self):
        d = g.seo_description(item())
        self.assertIn("スシロー", d)
        self.assertIn("246kcal", d)
        self.assertIn("600〜700円", d)
        self.assertLessEqual(len(d), 200)

    def test_page_title_has_site_name(self):
        h = g.render_item_page(item())
        self.assertIn("<title>スシロー「テストラテ」のカロリー・価格・アレルゲン｜回転寿司ポチ</title>", h)


class TestPrice(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(g.fmt_price(item(price=690, price_max=None)), "690円（税込）")
        self.assertEqual(g.fmt_price(item(price=530, price_max=590)), "530〜590円（税込）")
        self.assertEqual(g.fmt_price(item(price=4300, price_max=None)), "4,300円（税込）")
        self.assertEqual(g.fmt_price(item(price=570, price_max=None, price_from=True)), "570円〜（税込）")
        self.assertEqual(g.fmt_price(item(price=None)), "")

    def test_no_price_page_says_check_official(self):
        h = g.render_item_page(item(price=None, price_max=None, price_history=[]))
        self.assertIn("価格は公式サイトでご確認ください", h)

    def test_price_variants_table(self):
        h = g.render_item_page(item(price=4300, price_max=None, price_variants=[{"label": "販売価格", "price": 4300}, {"label": "宅配価格", "price": 5300}]))
        self.assertIn("宅配価格", h)
        self.assertIn("5,300円（税込）", h)


class TestPeriod(unittest.TestCase):
    def test_on_sale(self):
        self.assertEqual(g.period_text(item()), "2026-09-02〜 販売中")
        self.assertIn("終了予定", g.period_text(item(planned_end_text="お渡し期間：12月25日"), detailed=True))

    def test_ended(self):
        it = item(status="ended", ended_observed_at="2026-10-21")
        self.assertEqual(g.period_text(it), "2026-09-02〜2026-10-21")
        self.assertIn("消えたことを確認した日", g.period_text(it, detailed=True))

    def test_unknown_launch_uses_recording_start(self):
        self.assertIn("2026-11-03（記録開始日）", g.period_text(item(launch_date=None)))

    def test_kurasushi_listed_date_is_shown_as_site_listing_date(self):
        it = item(chain="kurasushi", launch_date=None, listed_at="2026-10-07")
        self.assertEqual(g.period_text(it), "2026-10-07（公式サイト掲載日）〜 販売中")
        self.assertEqual(g.effective_date(it), "2026-10-07")

    def test_relaunch_ignores_last_season_listed_date(self):
        it = item(chain="kurasushi", launch_date=None, listed_at="2025-10-07", relaunches=[{"ended_observed_at": "2025-12-01", "reappeared_at": "2026-10-05"}])
        self.assertIn("2026-10-05（記録開始日）", g.period_text(it))


class TestItemPage(unittest.TestCase):
    def test_no_description_quote_only_facts_and_links(self):
        h = g.render_item_page(item())
        self.assertNotIn("<blockquote", h)
        self.assertIn('href="https://menu.sushiro.example/9001"', h)  # 公式のメニューページ
        self.assertIn('href="https://news.example/p1"', h)  # 公式のお知らせ・トピック
        self.assertIn("出典：スシロー公式サイト（2026-11-03時点）", h)

    def test_no_images_except_site_logo(self):
        h = g.render_item_page(item())
        for src in re.findall(r'<img[^>]*src="([^"]*)"', h):
            # サイトのロゴと、運営者自身のブログ(ネタフル)のレビュー記事のアイキャッチだけ。各社の公式画像は表示しない
            self.assertTrue(src == "/assets/logo.png" or src.startswith("https://netaful.jp/"), src)
            for official in ("akindo-sushiro.co.jp", "kurasushi.co.jp", "hamazushi.com", "kappasushi.jp", "zensho.co.jp", "cmsimage"):
                self.assertNotIn(official, src)

    def test_nutrition_inline_for_few_other(self):
        h = g.render_item_page(item())
        self.assertIn("246kcal", h)
        self.assertIn("カロリー（Tall）", h)
        self.assertIn("飽和脂肪酸", h)
        self.assertNotIn("<details><summary>そのほかの栄養成分", h)

    def test_nutrition_details_for_many_other(self):
        other = {f"項目{i}": f"{i}mg" for i in range(10)}
        h = g.render_item_page(item(nutrition={"kcal": 520.0, "other": other}))
        self.assertIn("そのほかの栄養成分（10項目）", h)  # 栄養成分の表をもつ商品(今は使わないが表示は残してある)

    def test_kcal_only_variants_table(self):
        v = [{"label": "HOT Short", "nutrition": {"kcal": 231.0}}, {"label": "COLD Tall", "nutrition": {"kcal": 236.0}}]
        h = g.render_item_page(item(nutrition={"kcal": 231.0}, nutrition_variants=v, nutrition_label="HOT Short"))
        self.assertIn("HOT Short", h)
        self.assertIn("236kcal", h)

    def test_multi_column_variants(self):
        n = {"kcal": 200, "protein_g": 3.0, "fat_g": 5.0, "carbs_g": 30.0, "salt_g": 0.1}
        v = [{"label": "Short", "nutrition": n}, {"label": "Tall", "nutrition": {**n, "kcal": 250}}]
        h = g.render_item_page(item(nutrition_variants=v))
        self.assertIn("class='multi'", h)
        self.assertIn("<th>Short</th>", h)

    def test_missing_nutrition_points_to_official(self):
        h = g.render_item_page(item(nutrition=None, allergens=None))
        self.assertIn("カロリーは未取得です", h)
        self.assertIn("https://nutrition.example/", h)
        self.assertIn("アレルギー情報は未取得です", h)
        self.assertIn("https://allergen.example/", h)

    def test_allergens_contains_none_and_partial(self):
        h = g.render_item_page(item(allergens={"contains": [], "partial": [], "none_listed": True}))
        self.assertIn("公式の表で「含む」とされた品目はありません", h)
        h = g.render_item_page(item(allergens={"contains": ["卵"], "partial": [{"name": "えび", "mark": "△"}],
                                               "partial_meaning": "同じラインで製造しています"}))
        self.assertIn("条件つきの表示（△）：えび△（公式の凡例：同じラインで製造しています）", h)
        self.assertIn("出典：", h)
        self.assertIn("2026年10月22日更新", h)
        self.assertIn("最新のアレルギー情報は公式サイトでご確認ください", h)

    def test_price_history_only_when_changed(self):
        self.assertNotIn("価格の履歴", g.render_item_page(item()))
        hist = [{"date": "2026-11-03", "price": 600}, {"date": "2026-11-20", "price": 650, "price_max": 750}]
        h = g.render_item_page(item(price_history=hist, price=650))
        self.assertIn("価格の履歴", h)
        self.assertIn("650〜750円", h)

    def test_launch_date_note_only_when_unknown(self):
        self.assertNotIn("発売日は", g.render_item_page(item()))
        h = g.render_item_page(item(launch_date=None, news_url=None, news_title=None))
        self.assertIn("発売日は不明のため、記録を始めた日から表示しています", h)

    def test_ended_note_only_on_ended_items(self):
        self.assertNotIn("店頭ではまだ販売", g.render_item_page(item()))
        it = item(status="ended", ended_observed_at="2026-11-11")
        self.assertIn("店頭ではまだ販売していることもあります", g.render_item_page(it))

    def test_price_is_said_to_differ_by_store_on_every_item_page(self):
        for chain in ("sushiro", "kurasushi", "hamazushi", "kappasushi"):
            h = g.render_item_page(item(chain=chain))
            self.assertRegex(h, r"店舗(により|によって)")
        self.assertIn("巣本店", g.render_item_page(item(chain="sushiro")))

    def test_ended_by_official_close_date_is_labelled(self):
        it = item(chain="kappasushi", status="ended", ended_observed_at="2026-11-11", ended_basis="official_close_date")
        self.assertNotIn("店頭ではまだ販売", g.render_item_page(it))
        self.assertIn("終了日は公式のメニューデータの販売終了日", g.period_text(it, detailed=True))

    def test_hamazushi_kcal_comes_from_the_pdf_and_says_so(self):
        h = g.render_item_page(item(chain="hamazushi"))
        self.assertIn("カロリーは公式のアレルゲン一覧表（PDF）の値です", h)

    def test_sushiro_sellout_note(self):
        it = item(chain="sushiro", planned_end_text="2026-11-08まで（完売次第終了）")
        h = g.render_item_page(it)
        self.assertIn("終了予定：2026-11-08まで（完売次第終了）", h)

    def test_season_history_and_relaunch_notes(self):
        it = item(seasons=[{"launch_date": "2025-11-01", "ended_observed_at": "2026-01-10"}],
                  relaunches=[{"ended_observed_at": "2026-01-10", "reappeared_at": "2026-11-02"}])
        h = g.render_item_page(it)
        self.assertIn("昨季の掲載：2025-11-01〜2026-01-10", h)
        self.assertIn("2026-11-02に公式サイトの一覧へ再び登場しました", h)

    def test_no_adsense_when_unset(self):
        from unittest import mock
        with mock.patch.dict(g.CONFIG, {"adsense_client_id": "", "adsense_ad_slot": "",
                                         "ga_measurement_id": ""}):
            h = g.render_item_page(item())
        self.assertNotIn("adsbygoogle", h)
        self.assertNotIn("googletagmanager", h)

    def test_notes_come_after_nutrition(self):
        # 注意書きが先に並ぶと、カロリーまで長くスクロールすることになる
        it = item()
        h = g.render_item_page(it)
        nutrition = g.render_nutrition(it)
        self.assertTrue(nutrition)
        self.assertLess(h.index(nutrition), h.index("補足と出典"))
        self.assertLess(h.index("補足と出典"), h.index("<p class='source'>"))

    def test_nutrition_notes_are_short(self):
        h = g.render_item_page(item(nutrition_note="公式サイトで公表されているのはカロリー(kcal)のみです"))
        self.assertNotIn("公表されているのは", h)
        self.assertNotIn("転記", h)
        self.assertIn('href="https://nutrition.example', h)  # 栄養の出典は補足と出典に残す
        self.assertIn("最新のアレルギー情報は公式サイトでご確認ください", h)  # アレルゲンの案内は残す
        h = g.render_item_page(item(nutrition_note="ミルクは標準の値です"))
        self.assertIn("ミルクは標準の値です。", h)

    def test_ga4_tag_present_when_set(self):
        from unittest import mock
        with mock.patch.dict(g.CONFIG, {"ga_measurement_id": "G-TEST123"}):
            h = g.render_item_page(item())
        self.assertIn("googletagmanager.com/gtag/js?id=G-TEST123", h)


class TestChainColors(unittest.TestCase):
    def test_css_has_light_and_dark_chain_colors_and_dark_badge_text(self):
        css = g.build_css()
        for c in g.CHAINS:
            self.assertIn(f"--{c['slug']}: {c['color']};", css)
            self.assertIn(f"--{c['slug']}: {c['color_dark']};", css)
            self.assertIn(f'.item[data-chain="{c["slug"]}"] .badge', css)
        self.assertIn("--accent: #1E3A5F", css)
        self.assertIn("prefers-color-scheme: dark", css)
        self.assertIn(".chain-badge { color: #0e1520; }", css)


class TestAdSlots(unittest.TestCase):
    """広告(AdSense)。ヘッダー下1枠＋カード一覧の中(JSが差し込む。最大ADS_MAX_PER_GRID枠)、商品ページは1枠。"""

    def setUp(self):
        from unittest import mock
        p = mock.patch.dict(g.CONFIG, {"adsense_client_id": "ca-pub-1", "adsense_ad_slot": "2"})
        p.start()
        self.addCleanup(p.stop)
        self.items = [item(chain=c, product_id=f"{c}{i}") for c in ("sushiro", "kurasushi", "hamazushi") for i in range(3)]

    def pages(self):
        return {
            "grid": g.render_grid_page("週", "d", self.items, "https://x/"),
            "top": g.render_top(self.items),
            "item": g.render_item_page(self.items[0]),
        }

    def test_real_config_starts_without_ads(self):
        # 最初は広告を出さない。運営者が公開後に値を入れる(コードは残してある)
        real = json.loads((Path(g.ROOT) / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(real["adsense_client_id"], "")
        self.assertEqual(real["adsense_ad_slot"], "")

    def test_head_script_and_vignette_opt_out(self):
        for name, h in self.pages().items():
            self.assertIn("adsbygoogle.js?client=ca-pub-1", h.split("</head>")[0], name)
            self.assertNotIn("<a href", h, name)
            self.assertNotIn("<a class", h, name)

    def test_no_server_side_ad_inside_sortable_grid(self):
        for name, h in self.pages().items():
            for m in re.finditer(r'<div class="grid" data-sortable>(.*?)\n</div>\n', h, re.S):
                self.assertNotIn("ad-slot", m.group(1), name)
                self.assertNotIn("adsbygoogle", m.group(1), name)

    def test_grid_pages_have_ad_js_template_and_constants(self):
        for name in ("grid", "top"):
            h = self.pages()[name]
            self.assertIn("function syncAds()", h, name)
            self.assertIn(f"var AD_EVERY = {g.ADS_EVERY};", h, name)
            self.assertIn(f"var AD_MAX = {g.ADS_MAX_PER_GRID};", h, name)
            self.assertEqual(h.count('<template id="ad-template">'), 1, name)
            self.assertNotIn("__ADS_", h, name)
            self.assertGreater(h.rindex("syncAds();"), h.index("var AD_EVERY"), name)
            # ヘッダー下1枠 + 雛形1つ。実際の差し込み(最大3枠)はJS
            self.assertEqual(h.split("<body>")[1].count('<ins class="adsbygoogle"'), 2, name)
        self.assertEqual((g.ADS_EVERY, g.ADS_MAX_PER_GRID), (12, 3))

    def test_item_page_has_one_ad(self):
        h = self.pages()["item"]
        self.assertEqual(h.split("<body>")[1].count('<ins class="adsbygoogle"'), 1)
        self.assertNotIn("<template", h)
        self.assertEqual(h.count("adsbygoogle || []).push"), 1)  # syncAdsの1回だけ(枠ごとのinline pushなし)
        h2 = g.render_item_page(item(nutrition=None, allergens=None, price_history=[]))
        self.assertEqual(h2.split("<body>")[1].count('<ins class="adsbygoogle"'), 1)

    def test_nothing_when_unset(self):
        from unittest import mock
        with mock.patch.dict(g.CONFIG, {"adsense_client_id": "", "adsense_ad_slot": ""}):
            for name, h in self.pages().items():
                self.assertNotIn("adsbygoogle", h.replace(g.build_css(), ""), name)
                self.assertNotIn("function syncAds", h, name)
                self.assertNotIn("<template", h, name)
                self.assertNotIn('class="ad-slot"', h, name)
                self.assertNotIn("data-google-vignette", h, name)

    def test_one_side_unset_outputs_no_slot(self):
        from unittest import mock
        for key in ("adsense_client_id", "adsense_ad_slot"):
            with mock.patch.dict(g.CONFIG, {key: ""}):
                for name, h in self.pages().items():
                    self.assertNotIn('class="ad-slot"', h, name)

    def test_no_ads_on_empty_pages(self):
        self.assertNotIn('class="ad-slot"', g.render_grid_page("週", "d", [], "https://x/"))

    def test_scripts_are_valid_javascript(self):
        import os, shutil, subprocess, tempfile
        node = shutil.which("node") or "/opt/homebrew/opt/node@22/bin/node"
        if not os.path.exists(node):
            self.skipTest("node がない")
        for m in re.finditer(r"<script>(.*?)</script>", self.pages()["top"], re.S):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
                f.write(m.group(1))
            try:
                r = subprocess.run([node, "--check", f.name], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
            finally:
                os.unlink(f.name)

    def _run_ad_plan(self, cases):
        import json, os, shutil, subprocess
        node = shutil.which("node") or "/opt/homebrew/opt/node@22/bin/node"
        if not os.path.exists(node):
            self.skipTest("node がない")
        script = (
            g.AD_PLAN_JS
            + "\nconsole.log(JSON.stringify(%s.map(function (c) { return computeAdPlan(c, %d, %d); })));"
            % (json.dumps(cases), g.ADS_EVERY, g.ADS_MAX_PER_GRID)
        )
        r = subprocess.run([node, "-e", script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_compute_ad_plan(self):
        cases = [[0], [11], [12], [13], [30], [42, 65, 34, 11, 10],
                 [13, 13, 13, 13, 13], [212], [], [13, 5, 20]]
        got = self._run_ad_plan(cases)
        self.assertEqual(got[0], [[]])
        self.assertEqual(got[1], [[]])
        self.assertEqual(got[2], [[]])          # ちょうどAD_EVERY件は置かない
        self.assertEqual(got[3], [[12]])        # 13件: 末尾の1件手前
        self.assertEqual(got[4], [[12, 24]])    # 30件: 36は範囲外
        self.assertEqual(got[5], [[12], [12], [12], [], []])  # 置ける3社に1枠ずつ
        # 13件以上が5社: 等間隔に3社(0,1,3番目)を選び1枠ずつ
        self.assertEqual(got[6], [[12], [12], [], [12], []])
        self.assertEqual(got[7], [[12, 24, 36]])  # 単一グリッドは3枠
        self.assertEqual(got[8], [])
        self.assertEqual(got[9], [[12], [], [12]])
        for plan, counts in zip(got, cases):
            self.assertLessEqual(sum(len(p) for p in plan), g.ADS_MAX_PER_GRID)
            for p, n in zip(plan, counts):
                self.assertEqual(p, sorted(set(p)))
                for i in p:
                    self.assertTrue(0 < i < n)

    def test_place_ads_never_detaches_ad_slots(self):
        # 枠をDOMから外してよいのは「余った枠を捨てる」1か所だけ。並べ替えは.itemしか動かさない
        js = g.SORT_JS
        self.assertEqual(js.count("removeChild("), 1)
        self.assertIn("slots.pop()", js)
        self.assertNotIn("innerHTML", js)
        self.assertIn("el.classList.contains('ad-slot')", js)
        self.assertNotIn("__AD_PLAN__", js)
        self.assertIn("function computeAdPlan", js)


class TestTop(unittest.TestCase):
    def test_website_jsonld(self):
        h = g.render_top([item()])
        m = re.search(r'<script type="application/ld\+json">(.*?)</script>', h, re.S)
        ld = json.loads(m.group(1))
        self.assertEqual(ld["@type"], "WebSite")
        self.assertEqual(ld["name"], "回転寿司ポチ")
        self.assertEqual(h.count("application/ld+json"), 1)

    def test_favicon_sizes(self):
        h = g.render_top([item()])
        self.assertIn('<link rel="icon" type="image/png" sizes="96x96" href="/assets/favicon.png">', h)

    def test_favicon_file_is_96px(self):
        import struct
        data = (Path(g.ROOT) / "docs/assets/favicon.png").read_bytes()
        w, h = struct.unpack(">II", data[16:24])
        self.assertEqual((w, h), (96, 96))

    def test_groups_by_chain_and_order(self):
        a = item(product_id="1", name="古い", launch_date="2026-09-01")
        b = item(product_id="2", name="新しい", launch_date="2026-10-01")
        c = item(chain="kurasushi", product_id="3", name="くら寿司の", launch_date="2026-09-15")
        h = g.render_top([a, b, c])
        self.assertLess(h.index("新しい"), h.index("古い"))
        self.assertLess(h.index('data-chain="sushiro"'), h.index('data-chain="kurasushi"'))
        self.assertNotIn("社別", h)

    def test_sortable_grid_contains_only_cards(self):
        # 広告設定済みだと全<a>にdata-google-vignetteが付くので、外してから見る
        h = g.render_top([item(), item(product_id="2")]).replace(' data-google-vignette="false"', "")
        for m in re.finditer(r'<div class="grid" data-sortable>\n(.*?)\n</div>\n', h, re.S):
            inner = m.group(1)
            self.assertNotIn("ad-slot", inner)
            self.assertNotIn("<h", inner)
            # 直下の要素は <a class="item"> だけ
            self.assertEqual(len(re.findall(r'<a class="item"', inner)), 2)

    def test_status_filter_default_on_sale_and_ended_hidden_count(self):
        ended = item(product_id="2", status="ended", ended_observed_at="2026-10-06")
        h = g.render_top([item(), ended], today=__import__("datetime").date(2026, 10, 8))
        self.assertIn('<option value="on_sale" selected>', h)
        self.assertIn("販売中の期間限定ネタ (1件)", h)
        self.assertIn("価格は各社公式サイトの表示です（スシローは代表の1店舗・巣本店（大阪府門真市）の価格）", h)

    def test_old_ended_not_on_top(self):
        old = item(product_id="2", status="ended", ended_observed_at="2026-01-01", name="昔のメニュー")
        h = g.render_top([item(), old], today=__import__("datetime").date(2026, 10, 8))
        self.assertNotIn("昔のメニュー", h)

    def test_empty_site_has_no_empty_headings(self):
        h = g.render_top([])
        self.assertNotIn("<h3>", h)

    def test_about_is_short_and_states_facts_only_policy(self):
        h = g.render_top([item()]).replace(' data-google-vignette="false"', "")
        self.assertIn('<a href="https://netaful.jp/">ネタフル</a>', h)
        self.assertIn("非公式サイト", h)
        self.assertIn("記録しているのは事実情報だけ", h)
        self.assertIn("説明文と画像は載せていません", h)
        self.assertIn("各社の公式サイトをご覧ください", h)


class TestArchive(unittest.TestCase):
    def test_empty_archive(self):
        h = g.render_archive_index([item()])
        self.assertIn("まだ販売が終わったメニューはありません", h)
        self.assertNotIn("/archive/2026", h)

    def test_months_grouped_by_start_month(self):
        e1 = item(product_id="1", status="ended", ended_observed_at="2026-10-01", launch_date="2026-09-02")
        e2 = item(product_id="2", status="ended", ended_observed_at="2026-10-02", launch_date="2026-09-20")
        e3 = item(product_id="3", status="ended", ended_observed_at="2026-10-03", launch_date="2026-08-18")
        h = g.render_archive_index([e1, e2, e3, item(product_id="4")])
        self.assertIn('/archive/2026-09/', h)
        self.assertIn('/archive/2026-08/', h)
        self.assertLess(h.index("2026-09"), h.index("2026-08"))
        self.assertIn("2026年9月に始まった期間限定ネタ<span class=\"n\">2件", h)

    def test_card_shows_period(self):
        e = item(status="ended", ended_observed_at="2026-10-21")
        self.assertIn("2026-09-02〜2026-10-21", g.render_card(e))
        self.assertIn('data-status="ended"', g.render_card(e))


class TestOutputs(unittest.TestCase):
    def test_robots(self):
        r = g.generate_robots()
        for bot in ["GPTBot", "CCBot", "Google-Extended", "Applebot-Extended", "anthropic-ai", "ClaudeBot",
                    "Bytespider", "meta-externalagent", "cohere-training-data-crawler", "Diffbot", "Omgilibot"]:
            self.assertIn(f"User-agent: {bot}", r)
        self.assertIn("User-agent: *\nAllow: /", r)
        self.assertNotIn("Googlebot", r)
        self.assertIn("Sitemap: https://sushi.netaful.jp/sitemap.xml", r)

    def test_rss_and_sitemap(self):
        rss = g.generate_rss([item()])
        self.assertIn("【スシロー】テストラテ", rss)
        sm = g.generate_sitemap([item()], ["2026-09"])
        self.assertIn("https://sushi.netaful.jp/items/sushiro-9001.html", sm)
        self.assertIn("https://sushi.netaful.jp/archive/2026-09/", sm)

    def test_search_index(self):
        idx = g.build_search_index([item(status="ended")])
        self.assertEqual(idx[0]["s"], "ended")
        self.assertEqual(idx[0]["cn"], "スシロー")

    def test_main_generates_and_cleans(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            saved = (g.PRODUCTS, g.DOCS)
            g.PRODUCTS, g.DOCS = tmp / "products", tmp / "docs"
            try:
                (g.PRODUCTS / "sushiro").mkdir(parents=True)
                (g.PRODUCTS / "sushiro/9001.json").write_text(json.dumps(item()), encoding="utf-8")
                ended = item(product_id="9002", status="ended", ended_observed_at="2026-10-01")
                (g.PRODUCTS / "sushiro/9002.json").write_text(json.dumps(ended), encoding="utf-8")
                (g.DOCS / "items").mkdir(parents=True)
                (g.DOCS / "items/sushiro-old.html").write_text("x")
                (g.DOCS / "archive/2025-01").mkdir(parents=True)
                g.main()
                self.assertTrue((g.DOCS / "items/sushiro-9001.html").exists())
                self.assertFalse((g.DOCS / "items/sushiro-old.html").exists())  # 古いページは掃除
                self.assertFalse((g.DOCS / "archive/2025-01").exists())
                self.assertTrue((g.DOCS / "archive/2026-09/index.html").exists())
                self.assertEqual((g.DOCS / "CNAME").read_text().strip(), "sushi.netaful.jp")
                self.assertTrue((g.DOCS / ".nojekyll").exists())
                # くら寿司は0件でも空の見出しだけのページは出さない(「まだ記録がありません」の文だけ)
                tl = (g.DOCS / "chains/kurasushi/index.html").read_text(encoding="utf-8")
                self.assertNotIn('class="chain-group"', tl)
            finally:
                g.PRODUCTS, g.DOCS = saved


class TestUpdatedLabel(unittest.TestCase):
    """トップだけに「最終更新: 2026年10月14日 07:37」(日本時間・電書/家電/林檎ポチと同じ書式)を出す。"""

    def test_label_format_is_jst(self):
        import datetime
        utc = datetime.datetime(2026, 10, 13, 22, 37, tzinfo=datetime.timezone.utc)  # 日本時間 10/14 07:37
        self.assertEqual(g.updated_label(utc.astimezone(g.JST)), "最終更新: 2026年10月14日 07:37")

    def test_top_has_label_but_other_pages_do_not(self):
        top = g.render_top([item()])
        self.assertIn("最終更新: ", top)
        shell = g.page_shell("x", "d", "<p>b</p>", "https://example.invalid/")
        self.assertNotIn("最終更新", shell)  # 全ページに入れると毎日すべてのファイルが変わる


class TestCardLinksAreRootAbsolute(unittest.TestCase):
    """カードのリンクが /items/… になっている(階層のあるページ /chains/x/ などで404にならない)。"""

    def test_card_href_starts_with_slash(self):
        card = g.render_card(item())
        self.assertIn('href="/items/', card)
        self.assertNotIn('href="items/', card)

    def test_grid_page_has_no_relative_item_links(self):
        html = g.render_grid_page("チェーン", "d", [item()], "https://x/")
        self.assertNotIn('href="items/', html)


if __name__ == "__main__":
    unittest.main()
