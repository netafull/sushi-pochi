import unittest

import helpers  # noqa: F401
import fetch_netaful_reviews as nr
import generate_site as g


class TestTags(unittest.TestCase):
    def test_tag_ids_from_config(self):
        # 4社ともタグが無い(2026-11-03に /wp-json/wp/v2/tags?search= で確認)ので、記事検索+タイトル判定で代替する
        self.assertEqual(nr.TAGS, {"sushiro": None, "kurasushi": None, "hamazushi": None, "kappasushi": None})

    def test_tagless_chains_use_article_search_with_title_filter(self):
        self.assertEqual(nr.SEARCH_TERMS, {"sushiro": "スシロー", "kurasushi": "くら寿司", "hamazushi": "はま寿司", "kappasushi": "かっぱ寿司"})
        self.assertTrue(nr.title_passes_filter("kurasushi", "【くら寿司】新作デザートを食べてみた"))
        self.assertFalse(nr.title_passes_filter("kurasushi", "【はま寿司】「えび 塩ごま油」を食べてみた"))  # 本文にだけ語が出る記事
        self.assertTrue(nr.title_passes_filter("hamazushi", "はま寿司の新作"))
        self.assertFalse(nr.title_passes_filter("kappasushi", "セブンの新商品"))
        self.assertTrue(nr.title_passes_filter("sushiro", "スシローの皿が付録「おうちでスシロー すし皿BOOK」"))

    def test_all_chains_always_filtered(self):
        self.assertEqual(nr.TITLE_ALWAYS, {"sushiro", "kurasushi", "hamazushi", "kappasushi"})

    def test_search_url_orders_by_date(self):
        seen = {}
        def fake_get(url):
            seen["url"] = url
            return b"[]", {"X-WP-TotalPages": "1"}
        saved = nr.http_get
        nr.http_get = fake_get
        try:
            nr.fetch_tag_page(None, 1, 50, "くら寿司")
        finally:
            nr.http_get = saved
        self.assertIn("orderby=date", seen["url"])
        self.assertIn("search=", seen["url"])
        self.assertNotIn("tags=", seen["url"])


class TestMatching(unittest.TestCase):
    def test_names_match_strictness(self):
        n = nr.normalize_name
        self.assertTrue(nr.names_match(n("特製旨辛とんこつ醤油ラーメン"), n("特製旨辛とんこつ醤油ラーメン（平日限定）")))
        self.assertFalse(nr.names_match(n("ラーメン"), n("横浜家系ラーメン")))  # 6文字未満の部分一致は不可
        self.assertTrue(nr.names_match(n("大切りまぐろ"), n("大切りまぐろ")))

    def test_registered_mark_ignored(self):
        n = nr.normalize_name
        self.assertEqual(n("フラペチーノ® ティー"), n("フラペチーノ ティー"))

    def test_extract_name_from_title(self):
        a = nr.extract_article({"id": 1, "date": "2026-01-01T00:00:00", "link": "x",
                                "title": {"rendered": "【はま寿司】「特製旨辛とんこつ醤油ラーメン」食べてみた"}})
        self.assertEqual(a["product_name"], "特製旨辛とんこつ醤油ラーメン")

    def test_effective_date_prefers_launch_then_listed_then_first_seen(self):
        self.assertEqual(nr.product_effective_date({"launch_date": "2026-10-01", "listed_at": "2026-09-01", "first_seen_at": "2026-11-03T07:00"}), "2026-10-01")
        self.assertEqual(nr.product_effective_date({"listed_at": "2026-09-01", "first_seen_at": "2026-11-03T07:00"}), "2026-09-01")
        self.assertEqual(nr.product_effective_date({"first_seen_at": "2026-11-03T07:00:00+09:00"}), "2026-11-03")

    def test_keyword_longest_match_wins(self):
        kw = g.match_keyword_for_name("炙りミナミマグロ中とろ")
        self.assertEqual(kw["label"], "まぐろ・マグロ")  # 「炙り」(2)より「中とろ」「マグロ」が具体的
        self.assertEqual(g.match_keyword_for_name("濃厚あったか茶碗蒸し")["label"], "茶碗蒸し")
        self.assertIsNone(g.match_keyword_for_name("zzz"))

    def test_related_articles_prefer_same_chain(self):
        arts = {
            1: {"id": 1, "chain": "kurasushi", "title": "くら寿司のサーモン", "date": "2026-02-01"},
            2: {"id": 2, "chain": "sushiro", "title": "スシローのサーモン", "date": "2025-02-01"},
        }
        it = {"chain": "sushiro", "product_id": "9", "name": "炙りサーモン"}
        label, got = g.select_related_articles(it, arts)
        self.assertEqual(label, "サーモン")
        self.assertEqual([a["id"] for a in got], [2, 1])


if __name__ == "__main__":
    unittest.main()
