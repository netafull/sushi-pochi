"""公式アレルゲンPDFの表の読み取り(pdf_tables)のテスト。pdfminer も本物のPDFも使わず、合成の座標データで確かめる。"""
import unittest

import helpers  # noqa: F401
import pdf_tables as pt
import synth as S

CHAINS = {
    "sushiro": {"partial": "○", "updated": "2026/10/22更新　アレルギー情報"},
    "hamazushi": {"partial": "△", "updated": "更新日 2026/10/14"},
    "kurasushi": {"partial": "▲", "updated": "2026年10月30日現在"},
    "kappasushi": {"partial": None, "updated": "商品開発部門確認日 : 2026/7/27"},
}
N = {"sushiro": 170, "hamazushi": 220, "kurasushi": 100, "kappasushi": 170}


def build(chain, **kw):
    spec = pt.SPECS[chain]
    rows = S.make_rows(N[chain], partial=CHAINS[chain]["partial"], kcal=bool(spec["kcal_x"]))
    return S.pdf_pages(chain, S.ALLERGEN_COLUMNS, rows, CHAINS[chain]["updated"], **kw), rows


class TestParse(unittest.TestCase):
    def test_all_four_layouts_parse_and_validate(self):
        for chain in CHAINS:
            with self.subTest(chain=chain):
                pages, rows = build(chain)
                parsed = pt.parse_table(pages, chain)
                self.assertEqual(len(parsed["rows"]), len(rows), "行の数")
                self.assertEqual(parsed["column_sets"], [S.ALLERGEN_COLUMNS])
                self.assertIsNone(pt.validate_table(chain, parsed))
                self.assertRegex(parsed["updated"], r"^2026-\d\d-\d\d$")

    def test_marks_and_kcal_land_in_the_right_columns(self):
        for chain in CHAINS:
            with self.subTest(chain=chain):
                pages, rows = build(chain)
                parsed = pt.parse_table(pages, chain)
                by_name = {r["name"]: r for r in parsed["rows"]}
                r0 = by_name["テストネタ000"]  # 卵・小麦が●、えびが部分
                al = pt.allergens_from_row(chain, r0)
                self.assertEqual(al["contains"], ["卵", "小麦"])
                partial = CHAINS[chain]["partial"]
                self.assertEqual([p["name"] for p in al["partial"]], ["えび"] if partial else [])
                r1 = by_name["テストネタ001"]
                self.assertEqual(pt.allergens_from_row(chain, r1)["contains"], ["さけ", "大豆"])
                r3 = by_name["テストネタ003"]
                self.assertEqual(pt.allergens_from_row(chain, r3)["contains"], ["卵", "大豆"])
                if pt.SPECS[chain]["kcal_x"]:
                    self.assertEqual(r3["kcal"], 53.0)
                else:
                    self.assertIsNone(r3["kcal"])

    def test_multiple_pages_and_a_page_without_table(self):
        pages, rows = build("sushiro", per_page=40, extra_pages=1)
        parsed = pt.parse_table(pages, "sushiro")
        self.assertEqual(len(parsed["rows"]), len(rows))
        self.assertEqual(parsed["pages_table"], len(pages) - 1)

    def test_pages_may_have_different_column_orders(self):
        # かっぱ寿司のPDFは、掲載順を改定した版と古い版が同居する。行は自分のページの列で読む
        pages, rows = build("kappasushi", per_page=100)
        spec = pt.SPECS["kappasushi"]
        swapped = list(S.ALLERGEN_COLUMNS)
        swapped[0], swapped[2] = swapped[2], swapped[0]  # 卵 と 小麦 を入れ替え
        second = S.pdf_pages("kappasushi", swapped, [{"name": "入れ替えネタ", "kcal": None, "marks": {"卵": "●"}}],
                             "商品開発部門確認日 : 2026/7/27")
        parsed = pt.parse_table(pages + second, "kappasushi")
        self.assertEqual(len(parsed["column_sets"]), 2)
        row = [r for r in parsed["rows"] if r["name"] == "入れ替えネタ"][0]
        self.assertEqual(pt.allergens_from_row("kappasushi", row)["contains"], ["卵"])
        del spec

    def test_blank_cells_mean_no_mark_where_the_table_allows_it(self):
        pages, _ = build("sushiro")
        parsed = pt.parse_table(pages, "sushiro")
        self.assertTrue(any(r["marked"] == 0 for r in parsed["rows"]))
        self.assertIsNone(pt.validate_table("sushiro", parsed))


class TestVersion(unittest.TestCase):
    def test_latest_date_in_the_pdf_is_the_version(self):
        pages, _ = build("kappasushi", per_page=100)
        pages[0]["texts"].append([300, 560, 400, 570, "商品開発部門確認日 : 2026/10/22"])
        pages[0]["texts"].append([300, 540, 400, 550, "商品開発部門確認日 : 2026/9/8"])
        self.assertEqual(pt.parse_table(pages, "kappasushi")["updated"], "2026-10-22")


class TestValidation(unittest.TestCase):
    def test_too_few_rows(self):
        pages, _ = build("hamazushi")
        parsed = pt.parse_table(pages[:1], "hamazushi")
        self.assertIn("少なすぎ", pt.validate_table("hamazushi", parsed))

    def test_no_table_at_all(self):
        parsed = pt.parse_table([{"texts": [[0, 0, 5, 5, "x"]], "chars": [], "rules": []}], "sushiro")
        self.assertIn("見つかりません", pt.validate_table("sushiro", parsed))

    def test_missing_required_allergen_columns(self):
        cols = [c for c in S.ALLERGEN_COLUMNS if c != "えび"] + ["品目ぬ"]
        pages = S.pdf_pages("sushiro", cols, S.make_rows(170, partial="○"), "2026/10/22更新")
        parsed = pt.parse_table(pages, "sushiro")
        self.assertIn("必須の品目", pt.validate_table("sushiro", parsed))

    def test_column_count_out_of_range(self):
        pages = S.pdf_pages("sushiro", S.ALLERGEN_COLUMNS[:12], S.make_rows(170, partial="○"), "2026/10/22更新")
        self.assertIn("列数", pt.validate_table("sushiro", pt.parse_table(pages, "sushiro")))

    def test_missing_version_date(self):
        pages = S.pdf_pages("sushiro", S.ALLERGEN_COLUMNS, S.make_rows(170, partial="○"), "更新日なし")
        self.assertIn("更新日", pt.validate_table("sushiro", pt.parse_table(pages, "sushiro")))

    def test_kcal_column_missing_is_caught(self):
        rows = S.make_rows(220, partial="△", kcal=False)
        pages = S.pdf_pages("hamazushi", S.ALLERGEN_COLUMNS, rows, "更新日 2026/10/14")
        self.assertIn("カロリー", pt.validate_table("hamazushi", pt.parse_table(pages, "hamazushi")))


class TestMatching(unittest.TestCase):
    def rows(self, names_marks):
        return [{"name": n, "kcal": None, "cells": list(m), "columns": ["卵", "乳"], "marked": 0} for n, m in names_marks]

    def test_exact_after_normalization(self):
        rs = self.rows([("炙りサーモン（一貫）", ["●", None]), ("別のネタ", [None, "●"])])
        self.assertEqual(pt.rows_for_name(rs, "炙りサーモン(一貫)")[0]["name"], "炙りサーモン（一貫）")
        self.assertEqual(pt.rows_for_name(rs, "⾚えび") is None, True)

    def test_kangxi_radicals_are_normalized(self):
        rs = self.rows([("⼤切りまぐろ", ["●", None])])
        self.assertIsNotNone(pt.rows_for_name(rs, "大切りまぐろ"))

    def test_quantity_suffix_and_leading_note_are_loosened(self):
        rs = self.rows([("国産ほたて貝柱", ["●", None]), ("(北海道以外)しめさば 柚子おろし盛り", [None, "●"])])
        self.assertIsNotNone(pt.rows_for_name(rs, "国産ほたて貝柱 1貫"))
        self.assertIsNotNone(pt.rows_for_name(rs, "しめさば 柚子おろし盛り"))

    def test_hokkaido_only_rows_are_not_used_for_nationwide_products(self):
        rs = self.rows([("(北海道以外)しめさば", [None, "●"]), ("(北海道限定)しめさば", ["●", None])])
        hit = pt.rows_for_name(rs, "しめさば")
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["cells"], [None, "●"])

    def test_same_name_with_different_marks_is_ambiguous(self):
        rs = self.rows([("同名のネタ", ["●", None]), ("同名のネタ", [None, "●"])])
        self.assertIsNone(pt.rows_for_name(rs, "同名のネタ"))

    def test_same_name_with_same_marks_is_fine(self):
        rs = self.rows([("同名のネタ", ["●", None]), ("同名のネタ", ["●", None])])
        self.assertIsNotNone(pt.rows_for_name(rs, "同名のネタ"))

    def test_no_fuzzy_partial_match(self):
        rs = self.rows([("炙りサーモンチーズ", ["●", None])])
        self.assertIsNone(pt.rows_for_name(rs, "炙りサーモン"))

    def test_kura_none_column_is_not_an_allergen(self):
        row = {"name": "x", "kcal": 1, "cells": ["●", "●"], "columns": ["該当なし", "卵"], "marked": 2}
        self.assertEqual(pt.allergens_from_row("kurasushi", row)["contains"], ["卵"])


if __name__ == "__main__":
    unittest.main()
