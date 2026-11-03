"""発売日の照合(プレスリリース・お知らせの見出し/本文から)のテスト。"""
import unittest

import helpers  # noqa: F401
import crawl


def src(title, text="", date="2026-11-01", url="https://x.example/a"):
    return {"title": title, "text": text, "date": date, "url": url}


class TestFindLaunch(unittest.TestCase):
    def test_title_match(self):
        hit = crawl.find_launch("テスト季節ラテ", [src("新作「テスト季節ラテ」を11月2日（月）より発売")], "2026-11-03")
        self.assertEqual(hit["launch_date"], "2026-11-02")
        self.assertEqual(hit["url"], "https://x.example/a")

    def test_body_match_uses_date_near_the_name(self):
        text = "10月1日より別の商品を発売。◆テスト季節ラテ 11月2日（月）より発売。◆別商品 12月1日より発売"
        hit = crawl.find_launch("テスト季節ラテ", [src("お知らせ", text)], "2026-11-03")
        self.assertEqual(hit["launch_date"], "2026-11-02")

    def test_via_distinguishes_title_and_body(self):
        t = crawl.find_launch("テスト季節ラテ", [src("新作「テスト季節ラテ」を11月2日より発売")], "2026-11-03")
        self.assertEqual(t["via"], "title")
        b = crawl.find_launch("テスト季節ラテ", [src("お知らせ", "11月2日より。◆テスト季節ラテ")], "2026-11-03")
        self.assertEqual(b["via"], "body")
        p = {}
        crawl.apply_launch(p, b, "press_release")
        self.assertEqual(p["launch_date_source"], "press_release_body")
        crawl.apply_launch(p, t, "press_release")
        self.assertEqual(p["launch_date_source"], "press_release")

    def test_window_excludes_old_and_far_future_articles(self):
        old = src("「テスト季節ラテ」を11月2日より発売", date="2025-10-01")
        self.assertIsNone(crawl.find_launch("テスト季節ラテ", [old], "2026-11-03"))
        future = src("「テスト季節ラテ」を12月2日より発売", date="2026-12-01")
        self.assertIsNone(crawl.find_launch("テスト季節ラテ", [future], "2026-11-03"))

    def test_short_names_are_not_used(self):
        self.assertIsNone(crawl.find_launch("ラテ", [src("ラテを11月2日より発売")], "2026-11-03"))

    def test_parenthesis_suffix_and_store_prefix_are_ignored(self):
        hit = crawl.find_launch("テストケーキ（単品）", [src("「テストケーキ」を11月2日より発売")], "2026-11-03")
        self.assertEqual(hit["launch_date"], "2026-11-02")
        hit = crawl.find_launch("【店舗限定】テスト豆100％", [src("テスト豆100％を10月14日(水)より発売", date="2026-10-08")], "2026-10-20")
        self.assertEqual(hit["launch_date"], "2026-10-14")

    def test_longer_sibling_product_does_not_steal_the_match(self):
        names = ["ジョイフルテスト ティー", "ジョイフルテスト ティー ラテ", "ジョイフルテスト ティー ソーダ"]
        article = [src("ホリデー新作", "11月2日より「ジョイフルテスト ティー ラテ」と「ジョイフルテスト ティー ソーダ」を発売")]
        # 短い名前の商品は、長い名前の商品の記事で日付を取らない
        self.assertIsNone(crawl.find_launch(names[0], article, "2026-11-03", names))
        self.assertEqual(crawl.find_launch(names[1], article, "2026-11-03", names)["launch_date"], "2026-11-02")
        # 単独で出てくる記事があれば、それは採用する
        both = article + [src("ティー新作", "10月28日より「ジョイフルテスト ティー」を発売", date="2026-10-21", url="https://x.example/b")]
        self.assertEqual(crawl.find_launch(names[0], both, "2026-11-03", names)["launch_date"], "2026-10-28")

    def test_end_date_is_not_a_launch_date(self):
        self.assertIsNone(crawl.find_launch("テスト季節ラテ", [src("「テスト季節ラテ」は12月25日まで")], "2026-11-03"))


if __name__ == "__main__":
    unittest.main()
