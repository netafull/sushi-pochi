"""公式PDFの取り込み(pdf_update)のテスト。取得器・PDFの解析は偽物で、ネットワークも pdfminer も使わない。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401
import crawl
import pdf_update as pu
import synth as S
from fetch import BlockedError

NOW = "2026-11-03T07:00:00+09:00"


class FakePdf:
    """HEAD/GET の偽物。sig=None なら指紋の無いPDF(動的配信)。"""

    def __init__(self, sig="etag-1"):
        self.sig = sig
        self.heads = 0
        self.gets = 0
        self.head_error = None
        self.get_error = None

    def pdf_head(self, chain, url):
        self.heads += 1
        if self.head_error:
            raise self.head_error
        return {"etag": self.sig} if self.sig else {}

    def pdf_get(self, chain, url):
        self.gets += 1
        if self.get_error:
            raise self.get_error
        return b"%PDF-fake"


def make_extract(chain, rows, updated_text, columns=None, per_page=60):
    cols = columns or S.ALLERGEN_COLUMNS

    def extract(data, with_chars=False):
        return S.pdf_pages(chain, cols, rows, updated_text, per_page=per_page)
    return extract


def product(chain, pid, name, **kw):
    p = crawl.base_product(chain, pid, name, NOW, "2026-11-03")
    p.update(kw)
    return p


CFG = {"sushiro": {"key": "sushiro", "url": "https://x/sushiro.pdf", "label": "スシローPDF"},
       "hamazushi": {"key": "hamazushi", "url": "https://x/hama.pdf", "label": "はまPDF"},
       "kurasushi": {"key": "kurasushi", "url": "https://x/kura.pdf", "label": "くらPDF"}}


def run(chain, products, fetcher, state, pending, extract, day="2026-11-03", cache=None):
    with tempfile.TemporaryDirectory() as d:
        return pu.update_chain(chain, products, fetcher, state, pending, CFG[chain], day, NOW, extract=extract,
                               cache_dir=Path(cache or d))


class TestUpdate(unittest.TestCase):
    def setUp(self):
        self.rows = S.make_rows(170, partial="○", kcal=False)
        self.extract = make_extract("sushiro", self.rows, "2026/10/22更新　アレルギー情報")

    def test_first_run_fills_allergens_and_records_version(self):
        ps = [product("sushiro", "1", "テストネタ000"), product("sushiro", "2", "テストネタ003"),
              product("sushiro", "3", "名前の一致しない新ネタ")]
        f, state, pending = FakePdf(), {}, {}
        rep = run("sushiro", ps, f, state, pending, self.extract)
        self.assertEqual((f.heads, f.gets), (1, 1))
        self.assertEqual(rep["matched"], 2)
        self.assertEqual(ps[0]["allergens"]["contains"], ["卵", "小麦"])
        self.assertEqual(ps[0]["allergens"]["partial"][0]["mark"], "○")
        self.assertEqual(ps[1]["allergens"]["contains"], ["卵", "大豆"])
        self.assertEqual(ps[0]["allergen_source"]["version"], "2026-10-22")
        self.assertEqual(ps[0]["allergen_source"]["version_text"], "2026年10月22日更新")
        self.assertIsNone(ps[2]["allergens"])
        self.assertEqual(ps[2]["pdf_tried"], "2026-10-22")
        self.assertEqual(rep["unmatched"], ["名前の一致しない新ネタ"])
        self.assertEqual(state["sushiro"]["version"], "2026-10-22")
        self.assertEqual(state["sushiro"]["signature"], "etag-1||")
        self.assertNotIn("pdf:allergen", pending.get("sushiro", {}))

    def test_pdf_content_is_not_stored_in_products_or_state(self):
        ps = [product("sushiro", "1", "テストネタ000")]
        state = {}
        run("sushiro", ps, FakePdf(), state, {}, self.extract)
        blob = repr(ps) + repr(state)
        self.assertNotIn("テストネタ001", blob)  # 一致しなかった行は残さない

    def test_same_version_is_not_fetched_again_and_unmatched_not_retried(self):
        ps = [product("sushiro", "1", "テストネタ000"), product("sushiro", "3", "一致しない")]
        f, state = FakePdf(), {}
        run("sushiro", ps, f, state, {}, self.extract)
        self.assertEqual(f.gets, 1)
        rep = run("sushiro", ps, f, state, {}, self.extract)
        self.assertEqual((f.heads, f.gets), (2, 1))  # HEADだけ。この版で試した商品は再試行しない
        self.assertEqual(rep["changed"], [])

    def test_new_unmatched_product_triggers_one_fetch_with_cache(self):
        ps = [product("sushiro", "1", "テストネタ000")]
        f, state = FakePdf(), {}
        run("sushiro", ps, f, state, {}, self.extract)
        ps.append(product("sushiro", "9", "テストネタ010"))  # 後から記録された商品(PDFには行がある)
        with tempfile.TemporaryDirectory() as cache:
            run("sushiro", ps, f, state, {}, self.extract, cache=cache)
        self.assertIsNotNone(ps[1]["allergens"])
        self.assertEqual(f.gets, 2)

    def test_new_version_updates_values_and_keeps_a_change_note(self):
        ps = [product("sushiro", "1", "テストネタ000")]
        f, state = FakePdf("etag-1"), {}
        run("sushiro", ps, f, state, {}, self.extract)
        rows2 = S.make_rows(170, partial="○", kcal=False)
        rows2[0]["marks"] = {"卵": "●", "かに": "●"}
        f.sig = "etag-2"
        run("sushiro", ps, f, state, {}, make_extract("sushiro", rows2, "2026/11/02更新"))
        self.assertEqual(ps[0]["allergens"]["contains"], ["卵", "かに"])
        self.assertEqual(ps[0]["allergen_source"]["version"], "2026-11-02")
        self.assertEqual(ps[0]["allergen_changes"], [{"date": "2026-11-03"}])

    def test_same_version_never_overwrites(self):
        ps = [product("sushiro", "1", "テストネタ000")]
        f, state = FakePdf("etag-1"), {}
        run("sushiro", ps, f, state, {}, self.extract)
        ps[0]["allergens"]["contains"] = ["手で直した値"]
        f.sig = "etag-1b"  # 指紋だけ変わったが、PDFの更新日は同じ
        run("sushiro", ps, f, state, {}, self.extract)
        self.assertEqual(ps[0]["allergens"]["contains"], ["手で直した値"])

    def test_validation_failure_keeps_old_values_and_records_pending(self):
        ps = [product("sushiro", "1", "テストネタ000")]
        f, state, pending = FakePdf("etag-1"), {}, {}
        run("sushiro", ps, f, state, pending, self.extract)
        before = dict(ps[0]["allergens"])
        f.sig = "etag-2"
        broken = make_extract("sushiro", S.make_rows(10, partial="○", kcal=False), "2026/11/02更新")
        rep = run("sushiro", ps, f, state, pending, broken)
        self.assertEqual(ps[0]["allergens"], before)
        self.assertEqual(rep["changed"], [])
        self.assertIn("validation", pending["sushiro"]["pdf:allergen"]["reason"])
        self.assertEqual(state["sushiro"]["failed_signature"], "etag-2||")
        gets = f.gets
        run("sushiro", ps, f, state, pending, broken)
        self.assertEqual(f.gets, gets, "読み取れなかったのと同じファイルは取り直さない")

    def test_parse_exception_is_isolated(self):
        ps = [product("sushiro", "1", "テストネタ000")]

        def boom(data, with_chars=False):
            raise ValueError("壊れたPDF")
        pending = {}
        rep = run("sushiro", ps, FakePdf(), {}, pending, boom)
        self.assertEqual(rep["changed"], [])
        self.assertIn("parse_error", pending["sushiro"]["pdf:allergen"]["reason"])

    def test_blocked_head_stops_without_get(self):
        f = FakePdf()
        f.head_error = BlockedError("HTTP 403")
        pending = {}
        rep = run("sushiro", [product("sushiro", "1", "テストネタ000")], f, {}, pending, self.extract)
        self.assertTrue(rep["blocked"])
        self.assertEqual(f.gets, 0)
        self.assertIn("blocked", pending["sushiro"]["pdf:allergen"]["reason"])

    def test_head_failure_still_allows_fetch_when_never_fetched(self):
        f = FakePdf()
        f.head_error = OSError("timeout")
        ps = [product("sushiro", "1", "テストネタ000")]
        rep = run("sushiro", ps, f, {}, {}, self.extract)
        self.assertEqual(f.gets, 1)
        self.assertEqual(rep["matched"], 1)

    def test_no_signature_pdf_is_refetched_after_seven_days(self):
        ps = [product("sushiro", "1", "テストネタ000")]
        f, state = FakePdf(sig=None), {}
        run("sushiro", ps, f, state, {}, self.extract, day="2026-11-03")
        self.assertEqual(f.gets, 1)
        run("sushiro", ps, f, state, {}, self.extract, day="2026-11-05")
        self.assertEqual(f.gets, 1)
        run("sushiro", ps, f, state, {}, self.extract, day="2026-11-10")
        self.assertEqual(f.gets, 2)

    def test_missing_pdfminer_is_skipped_not_fatal(self):
        with mock.patch.object(pu, "have_pdfminer", return_value=False):
            with tempfile.TemporaryDirectory() as d:
                rep = pu.update_chain("sushiro", [product("sushiro", "1", "x")], FakePdf(), {}, {}, CFG["sushiro"],
                                      "2026-11-03", NOW, cache_dir=Path(d))
        self.assertIn("pdfminer", rep["skipped"])


class TestHamaKcal(unittest.TestCase):
    def test_kcal_comes_from_the_pdf_for_hamazushi_only(self):
        rows = S.make_rows(220, partial="△", kcal=True)
        extract = make_extract("hamazushi", rows, "更新日 2026/10/14")
        ps = [product("hamazushi", "a", "テストネタ003")]
        run("hamazushi", ps, FakePdf(), {}, {}, extract)
        self.assertEqual(ps[0]["nutrition"], {"kcal": 53.0})
        self.assertEqual(ps[0]["nutrition_label"], "1皿あたり")
        self.assertEqual(ps[0]["nutrition_source"]["version_text"], "2026年10月14日更新")
        self.assertEqual(ps[0]["allergens"]["contains"], ["卵", "大豆"])

    def test_kcal_change_is_noted(self):
        rows = S.make_rows(220, partial="△", kcal=True)
        ps = [product("hamazushi", "a", "テストネタ003")]
        f, state = FakePdf("e1"), {}
        run("hamazushi", ps, f, state, {}, make_extract("hamazushi", rows, "更新日 2026/10/14"))
        rows[3]["kcal"] = 99
        f.sig = "e2"
        run("hamazushi", ps, f, state, {}, make_extract("hamazushi", rows, "更新日 2026/11/20"))
        self.assertEqual(ps[0]["nutrition"], {"kcal": 99.0})
        self.assertEqual(ps[0]["nutrition_changes"][0]["from_kcal"], 53.0)


class TestKuraKcalCrossCheck(unittest.TestCase):
    def test_html_and_pdf_kcal_disagreement_aborts(self):
        rows = S.make_rows(100, partial="▲", kcal=True)  # PDFのカロリー = 50 + i
        extract = make_extract("kurasushi", rows, "2026年10月30日現在")
        ps = [product("kurasushi", str(i), f"テストネタ{i:03d}", nutrition={"kcal": 999.0}) for i in range(6)]
        pending = {}
        rep = run("kurasushi", ps, FakePdf(), {}, pending, extract)
        self.assertIn("食い違う", rep["aborted"])
        self.assertIsNone(ps[0]["allergens"])
        self.assertIn("pdf:allergen", pending["kurasushi"])

    def test_matching_kcal_passes_and_html_kcal_is_kept(self):
        rows = S.make_rows(100, partial="▲", kcal=True)
        extract = make_extract("kurasushi", rows, "2026年10月30日現在")
        ps = [product("kurasushi", str(i), f"テストネタ{i:03d}", nutrition={"kcal": float(50 + i)}) for i in range(6)]
        rep = run("kurasushi", ps, FakePdf(), {}, {}, extract)
        self.assertIsNone(rep["aborted"])
        self.assertEqual(ps[0]["allergens"]["contains"], ["卵", "小麦"])
        self.assertEqual(ps[0]["nutrition"], {"kcal": 50.0})


if __name__ == "__main__":
    unittest.main()
