"""Benchmarks: the arithmetic behind the table, and the crash matrix."""

from __future__ import annotations

import unittest

from stele import bench


class MeasurementTests(unittest.TestCase):
    def test_derived_rates(self):
        item = bench.Measurement(
            name="x",
            rows=100,
            seconds=0.5,
            pages_written=7,
            bytes_written=2048,
            logical_bytes=1024,
            page_count=9,
        )
        self.assertAlmostEqual(item.ops_per_second, 200.0)
        self.assertAlmostEqual(item.bytes_per_row, 20.48)
        self.assertAlmostEqual(item.amplification, 2.0)

    def test_row_formats_every_column(self):
        item = bench.Measurement("insert", 1000, 0.5, 7, 2048, 1024, 9)
        row = item.row()
        self.assertEqual(len(row), 7)
        self.assertEqual(row[0], "insert")
        self.assertEqual(row[1], "1,000")
        self.assertEqual(row[2], "2,000/s")
        self.assertEqual(row[3], "2 B")
        self.assertEqual(row[4], "2.0x")
        self.assertEqual(row[5], "500.0 ms")
        self.assertEqual(row[6], "7")

    def test_a_read_only_run_has_no_amplification(self):
        item = bench.Measurement("point-reads", 10, 0.1, 0, 0, 0, 3)
        self.assertEqual(item.bytes_per_row, 0.0)
        self.assertEqual(item.amplification, 0.0)
        self.assertEqual(item.row()[4], "-", "nothing was written, so nothing is amplified")

    def test_zero_seconds_does_not_divide_by_zero(self):
        item = bench.Measurement("x", 5, 0.0, 0, 0, 0, 2)
        self.assertEqual(item.ops_per_second, float("inf"))


class CrashReportTests(unittest.TestCase):
    def test_runs_count_both_kinds_of_tear(self):
        report = bench.CrashReport(points=3, old_state=5, new_state=1)
        self.assertEqual(report.runs, 6)
        self.assertTrue(report.ok)

    def test_an_unaccounted_run_is_a_failure(self):
        self.assertFalse(bench.CrashReport(points=3, old_state=5, new_state=0).ok)
        self.assertFalse(bench.CrashReport(points=1, old_state=1, new_state=1, failures=["x"]).ok)


class WorkloadTests(unittest.TestCase):
    def test_sequential_insert_writes_pages(self):
        item = bench.sequential_insert(300, batch=100)
        self.assertEqual(item.rows, 300)
        self.assertEqual(item.name, "insert-seq/b100")
        self.assertGreater(item.pages_written, 0)
        self.assertGreaterEqual(item.page_count, 3)
        self.assertEqual(item.logical_bytes, 300 * (10 + 32))

    def test_random_insert_uses_a_wider_key_space(self):
        item = bench.random_insert(200, seed=3)
        self.assertEqual(item.rows, 200)
        self.assertEqual(item.name, "insert-random")
        self.assertGreater(item.pages_written, 0)

    def test_point_reads_count_lookups_not_rows(self):
        item = bench.point_reads(300, 600)
        self.assertEqual(item.rows, 600)
        self.assertEqual(item.pages_written, 0, "reads must not write")

    def test_updates_report_the_rows_they_rewrote(self):
        item = bench.updates(100, rounds=3)
        self.assertEqual(item.rows, 300)
        self.assertGreater(item.pages_written, 0)

    def test_range_scan_counts_the_rows_it_returned(self):
        item = bench.range_scan(2000, span=50)
        self.assertEqual(item.rows, 50)
        self.assertEqual(item.name, "range-scan-50")
        self.assertEqual(item.pages_written, 0)

    def test_file_commits_pay_two_writes_per_row(self):
        item = bench.file_commits(rows=20)
        self.assertEqual(item.rows, 20)
        self.assertGreaterEqual(item.pages_written, 40, "one leaf page and one meta page per commit")
        self.assertEqual(item.page_count, item.pages_written)


class CrashMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = bench.crash_matrix(rows=20, extra=5)

    def test_every_crash_point_lands_on_one_of_two_databases(self):
        report = self.report
        self.assertTrue(report.ok, report.failures)
        self.assertEqual(report.verified, report.runs)
        self.assertEqual(report.old_state + report.new_state, report.runs)

    def test_both_outcomes_actually_occur(self):
        self.assertGreater(self.report.old_state, 0, "a crash before the meta write keeps the old root")
        self.assertGreater(self.report.new_state, 0, "a crash after it publishes the new one")
        self.assertEqual(self.report.torn, self.report.points, "every point is tried torn")

    def test_the_commit_is_longer_than_one_write(self):
        self.assertGreaterEqual(self.report.points, 2)

    def test_describe_reports_the_counts(self):
        lines = bench.describe(self.report)
        self.assertIn("page writes", lines[0])
        self.assertIn(str(self.report.runs), lines[1])
        self.assertEqual(len(lines), 6)


class EffectTests(unittest.TestCase):
    def test_batching_cuts_the_writes_per_row(self):
        effect = bench.batch_effect(200, sizes=(1, 200))
        (small, per_small, _), (big, per_big, _) = effect
        self.assertEqual((small, big), (1, 200))
        self.assertGreater(per_small, per_big * 10)

    def test_reclaim_empties_the_tree(self):
        lines = bench.reclaim(rows=200)
        self.assertEqual(len(lines), 4)
        self.assertIn("tree is empty: True", lines[3])
        self.assertIn("verified: True", lines[3])

    def test_footprint_compares_batch_sizes(self):
        lines = bench.footprint(rows=500, sizes=(1, 500))
        self.assertEqual(len(lines), 3)
        self.assertIn("page writes", lines[1])
        self.assertIn("of logical data", lines[0])

    def test_totals_add_up(self):
        items = [bench.sequential_insert(50), bench.point_reads(50, 100)]
        total = bench.totals(items)
        self.assertEqual(total["rows"], 150)
        self.assertAlmostEqual(total["seconds"], sum(item.seconds for item in items))
        self.assertEqual(total["pages"], sum(item.pages_written for item in items))


class SuiteTests(unittest.TestCase):
    def test_three_suites_are_registered(self):
        self.assertEqual(bench.suites(), ["quick", "core", "file"])
        self.assertEqual(set(bench.SUITE_HELP), set(bench.SUITES))

    def test_an_unknown_suite_lists_the_real_ones(self):
        with self.assertRaises(KeyError) as caught:
            bench.run("turbo")
        self.assertIn("quick", str(caught.exception))

    def test_the_quick_suite_runs_end_to_end(self):
        measurements = bench.run("quick")
        self.assertEqual(len(measurements), len(bench.SUITES["quick"]))
        for item in measurements:
            self.assertGreater(item.rows, 0)
            self.assertGreaterEqual(item.seconds, 0.0)


if __name__ == "__main__":
    unittest.main()
