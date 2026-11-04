"""巡回の共通部分(販売状況の追跡・歯止め・急減の検知・実行要約・連続日数・設定)のテスト。ネットワーク不使用。"""
import json
import unittest
from unittest import mock

import helpers
from helpers import FakeSite, TempData, run_chain, BlockedError
import crawl
import synth as S


def new_prod(**kw):
    p = crawl.base_product("sushiro", "1", "テスト", "2026-11-03T07:00:00+09:00", "2026-11-03")
    crawl.set_initial_price(p, 500, None, "2026-11-03")
    p.update(kw)
    return p


class TestConfig(unittest.TestCase):
    def test_user_agent_is_plain_and_honest(self):
        self.assertEqual(crawl.UA, "SushiPochi/1.0 (+https://sushi.netaful.jp/)")
        self.assertNotIn("Mozilla", crawl.UA)

    def test_intervals_at_least_one_second_and_limits_per_chain(self):
        chains = [c["slug"] for c in crawl.CONFIG["chains"]]
        self.assertEqual(sorted(chains), sorted(crawl.CRAWLERS))
        for c in chains:
            self.assertGreaterEqual(max(crawl.INTERVALS.get(c, 2), crawl.MIN_INTERVAL), 1)
            self.assertIn(c, crawl.MAX_REQUESTS)

    def test_throttle_never_below_one_second(self):
        with mock.patch.dict(crawl.INTERVALS, {"sushiro": 0}):
            t = crawl.Throttle()
            with mock.patch.object(crawl.time, "sleep") as sl, mock.patch.object(crawl.time, "monotonic", side_effect=[0.0, 0.0, 0.2, 0.2]):
                t.wait("sushiro")
                t.wait("sushiro")
            slept = sl.call_args.args[0]
            self.assertGreaterEqual(slept, 0.8 - 1e-9)

    def test_getter_enforces_request_cap(self):
        g = crawl.Getter()
        g.throttle.wait = lambda chain: None
        with mock.patch.dict(crawl.MAX_REQUESTS, {"hamazushi": 2}), mock.patch.object(crawl, "fetch", return_value="x"):
            g("hamazushi", "https://a/1")
            g("hamazushi", "https://a/2")
            with self.assertRaises(RuntimeError):
                g("hamazushi", "https://a/3")

    def test_site_titles(self):
        self.assertEqual(crawl.CONFIG["top_title"], "回転寿司ポチ｜期間限定ネタのカロリー・価格アーカイブ")
        self.assertEqual(crawl.CONFIG["site_title"], "回転寿司ポチ")

    def test_site_identity_and_related_sites(self):
        self.assertEqual(crawl.CONFIG["site_url"], "https://sushi.netaful.jp/")
        self.assertEqual(crawl.CONFIG["site_accent"], "#1E3A5F")
        urls = [r["url"] for r in crawl.CONFIG["related_sites"]]
        self.assertIn("https://cafe.netaful.jp/", urls)
        self.assertIn("https://fastfood.netaful.jp/", urls)

    def test_hamazushi_color_is_distinguishable_from_sushiro(self):
        colors = {c["slug"]: c["color"] for c in crawl.CONFIG["chains"]}
        self.assertEqual(colors["sushiro"], "#E60012")
        self.assertNotEqual(colors["hamazushi"], "#D7000F")  # スシローの赤と近いので調整してある

    def test_every_chain_has_a_pdf_config_and_a_parser_spec(self):
        import pdf_tables
        for c in crawl.CONFIG["chains"]:
            cfg = crawl.CONFIG["pdf"][c["slug"]]
            self.assertTrue(cfg["url"].startswith("https://"))
            self.assertIn(cfg["key"], pdf_tables.SPECS)

    def test_chain_colors_have_dark_variants(self):
        for c in crawl.CONFIG["chains"]:
            self.assertRegex(c["color"], r"^#[0-9A-Fa-f]{6}$")
            self.assertRegex(c["color_dark"], r"^#[0-9A-Fa-f]{6}$")


class TestTouchSeen(unittest.TestCase):
    def test_price_history_only_on_change(self):
        p = new_prod()
        crawl.touch_seen(p, 500, None, "2026-11-04")
        self.assertEqual(len(p["price_history"]), 1)
        crawl.touch_seen(p, 550, 600, "2026-11-05")
        self.assertEqual(p["price_history"][-1], {"date": "2026-11-05", "price": 550, "price_max": 600})
        self.assertEqual((p["price"], p["price_max"]), (550, 600))
        crawl.touch_seen(p, None, None, "2026-11-06")  # 価格が取れない日は変えない
        self.assertEqual(p["price"], 550)

    def test_relaunch_moves_last_season_to_seasons(self):
        p = new_prod(launch_date="2026-11-02", news_title="見出し", news_url="https://x")
        p["status"], p["ended_observed_at"] = "ended", "2026-12-20"
        crawl.touch_seen(p, 500, None, "2027-11-01")
        self.assertEqual(p["status"], "on_sale")
        self.assertEqual(p["seasons"][0]["launch_date"], "2026-11-02")
        self.assertEqual(p["seasons"][0]["ended_observed_at"], "2026-12-20")
        self.assertIsNone(p["launch_date"])
        self.assertEqual(p["relaunches"][0]["reappeared_at"], "2027-11-01")

    def test_missing_counts_reset_when_back(self):
        p = new_prod(missed_runs=1, last_miss_date="2026-11-04")
        crawl.touch_seen(p, 500, None, "2026-11-05")
        self.assertEqual((p["missed_runs"], p["last_miss_date"]), (0, None))


class TestRatioGuard(unittest.TestCase):
    def test_sudden_drop_is_ignored_for_two_days_then_accepted(self):
        with TempData():
            self.assertTrue(crawl.ratio_ok("hamazushi", 20, "2026-11-01"))
            self.assertFalse(crawl.ratio_ok("hamazushi", 5, "2026-11-02"))
            self.assertFalse(crawl.ratio_ok("hamazushi", 5, "2026-11-02"))  # 同じ日は数えない
            self.assertFalse(crawl.ratio_ok("hamazushi", 5, "2026-11-03"))
            self.assertTrue(crawl.ratio_ok("hamazushi", 5, "2026-11-04"))  # 3日続いたので本当の入れ替えとして受け入れる
            self.assertTrue(crawl.ratio_ok("hamazushi", 5, "2026-11-05"))

    def test_small_lists_are_exempt(self):
        with TempData():
            self.assertTrue(crawl.ratio_ok("kurasushi", 6, "2026-11-01"))
            self.assertTrue(crawl.ratio_ok("kurasushi", 2, "2026-11-02"))

    def test_drop_does_not_end_products(self):
        with TempData():
            # 10品が公式データにあり、翌日から2品に急減しても、3日目までは終了判定しない
            codes = [f"0{i:04d}" for i in range(10)]

            def site(use):
                prods = [S.kappa_product(c, f"テストネタ{c}") for c in codes]
                s = FakeSite()
                s.set(S.KAPPA_MENU_JSON, S.kappa_menu_json(prods))
                s.set(S.KAPPA_CAT_JSON, S.kappa_cat_json(use))
                return s
            run_chain(crawl.crawl_kappasushi, site(codes), "2026-11-01")
            low = site(codes[:2])
            for d in ("2026-11-02", "2026-11-03"):
                _, st = run_chain(crawl.crawl_kappasushi, low, d)
                self.assertFalse(st["kappasushi"]["list_ok"])
            self.assertTrue(all(p["status"] == "on_sale" for p in crawl.load_chain_products("kappasushi")))
            run_chain(crawl.crawl_kappasushi, low, "2026-11-04")  # 3日目に受け入れ(1回目の消失として数える)
            run_chain(crawl.crawl_kappasushi, low, "2026-11-05")
            ended = [p for p in crawl.load_chain_products("kappasushi") if p["status"] == "ended"]
            self.assertEqual(len(ended), 8)


class TestRobots(unittest.TestCase):
    def run_robots(self, text, path):
        site = FakeSite({"https://h.example/robots.txt": text} if text is not None else {})
        st = {"list_requests": 0}
        r = crawl.Robots(site, "sushiro", st)
        return r.allowed("https://h.example", path), st, site

    def test_disallow_is_respected(self):
        ok, st, site = self.run_robots("User-agent: *\nDisallow: /private/", "/private/x")
        self.assertFalse(ok)
        self.assertEqual(st["list_requests"], 1)

    def test_404_and_html_mean_allow_all(self):
        self.assertTrue(self.run_robots(None, "/anything")[0])
        self.assertTrue(self.run_robots("<html><body>x</body></html>", "/anything")[0])

    def test_fetched_once_per_host(self):
        site = FakeSite({"https://h.example/robots.txt": "User-agent: *\nDisallow:"})
        r = crawl.Robots(site, "sushiro", {"list_requests": 0})
        r.allowed("https://h.example", "/a")
        r.allowed("https://h.example", "/b")
        self.assertEqual(site.count("robots.txt"), 1)

    def test_blocked_robots_raises(self):
        site = FakeSite()
        site.blocked_hosts.add("h.example")
        with self.assertRaises(BlockedError):
            crawl.Robots(site, "sushiro", {"list_requests": 0}).allowed("https://h.example", "/a")


class TestRunSummary(unittest.TestCase):
    def test_fetch_failure_is_list_failed(self):
        def down(chain, url):
            raise OSError("down")
        _, stats = run_chain(crawl.crawl_sushiro, down, "2026-11-03")
        self.assertEqual(crawl.collect_problems(stats), {"blocked": [], "list_failed": ["sushiro"], "errors": []})

    def test_blocked_is_not_list_failed(self):
        def blocked(chain, url):
            raise BlockedError("HTTP 403")
        _, stats = run_chain(crawl.crawl_sushiro, blocked, "2026-11-03")
        self.assertEqual(crawl.collect_problems(stats), {"blocked": ["sushiro"], "list_failed": [], "errors": []})

    def test_suspected_restructure_is_not_list_failed(self):
        stats = {"sushiro": {"list_ok": False, "suspected_restructure": {"count": 40}}}
        self.assertEqual(crawl.collect_problems(stats)["list_failed"], [])

    def test_healthy_chain_has_no_problem_and_detail_failure_is_only_counted(self):
        stats = {"kurasushi": {"list_ok": True, "failed": 3}}
        self.assertEqual(crawl.collect_problems(stats), {"blocked": [], "list_failed": [], "errors": []})
        with TempData() as base:
            crawl.save_run_summary(stats)
            data = json.loads((base / "run_summary.json").read_text())
        self.assertEqual(data["detail_failed"], {"kurasushi": 3})

    def test_summary_file_has_new_fields(self):
        stats = {"kurasushi": {"list_ok": False, "blocked": True, "failed": 2},
                 "kappasushi": {"list_ok": False, "failed": 1},
                 "hamazushi": {"list_ok": False, "error": "RuntimeError: x"},
                 "sushiro": {"list_ok": True, "new_products": 1}}
        with TempData() as base:
            crawl.save_run_summary(stats, {"kappasushi": 2})
            data = json.loads((base / "run_summary.json").read_text())
        self.assertEqual(data["blocked"], ["kurasushi"])
        self.assertEqual(data["list_failed"], ["kappasushi"])
        self.assertEqual(data["errors"], ["hamazushi"])
        self.assertEqual(data["streaks"], {"kappasushi": 2})
        self.assertEqual(data["new"]["sushiro"], 1)

    def test_main_records_exception_as_error_and_streak(self):
        def boom(getter, pending, stats, today=None):
            raise RuntimeError("bug")
        with TempData() as base, mock.patch.dict(crawl.CRAWLERS, {"kurasushi": boom}), mock.patch.object(crawl, "Getter", mock.MagicMock()):
            self.assertEqual(crawl.main(["kurasushi"]), 0)
            data = json.loads((base / "run_summary.json").read_text())
            pend = json.loads((base / "pending.json").read_text())
        self.assertEqual(data["errors"], ["kurasushi"])
        self.assertEqual(data["streaks"], {"kurasushi": 1})
        self.assertEqual(pend["_health"]["kurasushi"]["days"], 1)

    def test_main_rejects_unknown_chain(self):
        self.assertEqual(crawl.main(["mcdonalds"]), 2)


class TestHealthStreak(unittest.TestCase):
    def test_streak_counts_by_date(self):
        pending = {}
        bad = {"kappasushi": {"list_ok": False}}
        self.assertEqual(crawl.update_health(pending, bad, "2026-11-01"), {"kappasushi": 1})
        self.assertEqual(crawl.update_health(pending, bad, "2026-11-01"), {"kappasushi": 1})  # 同日の再実行
        self.assertEqual(crawl.update_health(pending, bad, "2026-11-02"), {"kappasushi": 2})
        self.assertEqual(crawl.update_health(pending, bad, "2026-11-03"), {"kappasushi": 3})
        self.assertEqual(crawl.update_health(pending, bad, "2026-11-06"), {"kappasushi": 1})  # 空いたら数え直し

    def test_recovery_clears_and_untouched_kept(self):
        pending = {}
        crawl.update_health(pending, {"kurasushi": {"blocked": True}, "kappasushi": {"list_ok": False}}, "2026-11-01")
        crawl.update_health(pending, {"kurasushi": {"list_ok": True}}, "2026-11-02")
        self.assertNotIn("kurasushi", pending["_health"])
        self.assertEqual(pending["_health"]["kappasushi"]["days"], 1)
        crawl.update_health(pending, {"kappasushi": {"list_ok": True}}, "2026-11-02")
        self.assertNotIn("_health", pending)


class TestBurstFilterUnit(unittest.TestCase):
    TYPICAL = (("sushiro", 13), ("kurasushi", 50), ("hamazushi", 34), ("kappasushi", 25))

    def test_full_turnover_of_the_list_exceeds_the_threshold_for_every_chain(self):
        # 一覧の全品が一度に新規になる(ID体系の変更)のは、どのチェーンでも閾値を超える
        for chain, size in self.TYPICAL:
            self.assertGreater(size, crawl.burst_threshold(chain, size), chain)

    def test_normal_fair_rotation_does_not_trip_the_guard(self):
        # スシロー2026-10-28は13品中10品が一度に新規。通常のフェアの入れ替えで保留しない
        self.assertGreaterEqual(crawl.burst_threshold("sushiro", 13), 10)
        self.assertGreaterEqual(crawl.burst_threshold("kurasushi", 50), 45)
        self.assertGreaterEqual(crawl.burst_threshold("hamazushi", 34), 30)

    def test_absolute_cap_catches_a_runaway_classification(self):
        # 記録対象の判定が崩れて通常メニューまで限定扱いになる(一覧が数百件)桁違いの増え方は、割合が低くても保留
        self.assertEqual(crawl.burst_threshold("kurasushi", 223), 100)
        self.assertEqual(crawl.burst_threshold("sushiro", 219), 40)

    def test_first_run_exempt_and_escalation_days(self):
        with TempData():
            stats = {"hamazushi": crawl.new_stats()}
            items = {f"n{i}": {} for i in range(34)}
            self.assertEqual(crawl.burst_filter("hamazushi", items, None, {}, stats, "2026-11-01"), items)
            pending = {}
            for n, d in enumerate(("2026-11-02", "2026-11-03", "2026-11-04"), start=1):
                crawl.burst_filter("hamazushi", items, {"old"}, pending, stats, d)
                self.assertEqual(pending["hamazushi"][crawl.BURST_KEY]["consecutive_days"], n)
            self.assertFalse(stats["hamazushi"]["list_ok"])


if __name__ == "__main__":
    unittest.main()
