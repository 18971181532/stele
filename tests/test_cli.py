"""The command line contract: output, JSON, and four exit codes.

Most assertions call ``cli.main`` in-process, because the contract is about
return codes and text, not about process startup.  A handful of tests spawn
``run.py`` for real, so that the entry point and ``sys.exit`` are covered too.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest

from stele import cli
from stele import page as pg
from stele import render
from stele.disk import FileDisk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUN = os.path.join(ROOT, "run.py")


def call(*args: str):
    """Run one command in-process.  Returns (code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(list(args))
        except SystemExit as exit_:  # argparse: --version and usage errors
            code = int(exit_.code or 0)
    return code, out.getvalue(), err.getvalue()


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = os.path.join(self.directory.name, "store.db")

    def put(self, key: str, value: str) -> None:
        code, _, err = call("put", self.db, key, value)
        self.assertEqual(code, 0, err)

    def fill(self, count: int) -> None:
        for index in range(count):
            self.put(f"key-{index:03d}", f"value-{index}")


class EntryPointTests(CliTestCase):
    """The real thing: a subprocess, exactly as a user would run it."""

    def stele(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, RUN, *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )

    def test_version_is_printed_and_exits_zero(self):
        result = self.stele("--version")
        self.assertEqual(result.returncode, 0)
        self.assertRegex(result.stdout.strip(), r"^stele \d+\.\d+\.\d+$")

    def test_put_then_get_across_two_processes(self):
        put = self.stele("put", self.db, "name", "ada")
        self.assertEqual(put.returncode, 0, put.stderr)
        self.assertIn("put 'name'", put.stdout)
        self.assertIn("in transaction 1", put.stdout)

        got = self.stele("get", self.db, "name")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout.strip(), "ada")

    def test_a_missing_key_exits_three(self):
        got = self.stele("get", self.db, "nobody")
        self.assertEqual(got.returncode, cli.EXIT_NO)
        self.assertIn("no value for 'nobody'", got.stdout)
        self.assertEqual(got.stderr, "", "a missing key is not an error message")

    def test_a_usage_error_exits_two(self):
        self.assertEqual(self.stele("get").returncode, cli.EXIT_USAGE)
        self.assertEqual(self.stele("nonsense").returncode, cli.EXIT_USAGE)


class PutGetTests(CliTestCase):
    def test_put_json_reports_the_transaction(self):
        code, out, err = call("put", self.db, "city", "杭州", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"key": "city", "bytes": 6, "txid": 1})

    def test_get_json_distinguishes_found_from_empty(self):
        self.put("note", "")
        code, out, _ = call("get", self.db, "note", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"key": "note", "found": True, "value": ""})
        code, out, _ = call("get", self.db, "other", "--json")
        self.assertEqual(code, cli.EXIT_NO)
        self.assertEqual(json.loads(out), {"key": "other", "found": False})

    def test_utf8_keys_and_values_survive_the_round_trip(self):
        self.put("城市", "杭州")
        code, out, _ = call("get", self.db, "城市")
        self.assertEqual((code, out.strip()), (0, "杭州"))

    def test_a_second_put_advances_the_transaction(self):
        self.put("a", "1")
        code, out, _ = call("put", self.db, "b", "2", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["txid"], 2)


class DeleteTests(CliTestCase):
    def test_delete_reports_whether_the_key_was_there(self):
        self.put("role", "admin")
        code, out, _ = call("del", self.db, "role")
        self.assertEqual(code, 0)
        self.assertIn("deleted 'role'", out)

        code, out, _ = call("del", self.db, "role")
        self.assertEqual(code, cli.EXIT_NO)
        self.assertIn("no such key", out)

    def test_delete_json_carries_the_flag(self):
        self.put("role", "admin")
        _, out, _ = call("del", self.db, "role", "--json")
        self.assertEqual(json.loads(out)["removed"], True)
        _, out, _ = call("del", self.db, "role", "--json")
        self.assertEqual(json.loads(out)["removed"], False)

    def test_deleting_everything_leaves_a_verifiable_empty_store(self):
        self.fill(5)
        for index in range(5):
            self.assertEqual(call("del", self.db, f"key-{index:03d}")[0], 0)
        code, out, _ = call("verify", self.db)
        self.assertEqual(code, 0)
        self.assertIn("0 keys", out)
        self.assertEqual(call("scan", self.db)[0], cli.EXIT_NO)


class ScanTests(CliTestCase):
    def test_scan_is_ordered_and_prints_a_table(self):
        for key in ("c", "a", "b"):
            self.put(key, key.upper())
        code, out, err = call("scan", self.db)
        self.assertEqual(code, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["key", "value"])
        self.assertEqual([line.split()[0] for line in lines[2:]], ["a", "b", "c"])

    def test_bounds_and_limit(self):
        self.fill(10)
        _, out, _ = call("scan", self.db, "--low", "key-003", "--high", "key-006")
        self.assertEqual(out.count("value-"), 3)
        _, out, _ = call("scan", self.db, "--limit", "2")
        self.assertEqual(out.count("value-"), 2)

    def test_json_rows_keep_their_order(self):
        for key in ("b", "a"):
            self.put(key, key * 2)
        _, out, _ = call("scan", self.db, "--json")
        self.assertEqual(
            json.loads(out)["rows"],
            [{"key": "a", "value": "aa"}, {"key": "b", "value": "bb"}],
        )

    def test_an_empty_range_exits_three(self):
        self.put("only", "1")
        code, out, _ = call("scan", self.db, "--low", "a", "--high", "b")
        self.assertEqual(code, cli.EXIT_NO)
        self.assertIn("no rows in range", out)
        code, out, _ = call("scan", self.db, "--low", "a", "--high", "b", "--json")
        self.assertEqual(code, cli.EXIT_NO)
        self.assertEqual(json.loads(out), {"rows": []})

    def test_cjk_values_do_not_break_the_columns(self):
        self.put("a", "杭州")
        self.put("b", "x")
        lines = call("scan", self.db)[1].splitlines()
        cjk, ascii_ = lines[2], lines[3]
        self.assertIn("杭州", cjk)
        self.assertEqual(
            render.display_width(cjk[: cjk.index("杭州")]),
            render.display_width(ascii_[: ascii_.index("x")]),
            "both values start in the same column, counted in terminal cells",
        )


class StatTests(CliTestCase):
    def test_stat_counts_pages_and_keys(self):
        self.fill(20)
        code, out, err = call("stat", self.db, "--deep")
        self.assertEqual(code, 0, err)
        self.assertIn("txid      20", out)
        self.assertIn("pages     4", out)
        self.assertIn("items     20 keys", out)
        self.assertIn("pages on disk", out)

    def test_stat_json_is_machine_readable(self):
        self.fill(3)
        _, out, _ = call("stat", self.db, "--json")
        payload = json.loads(out)
        self.assertEqual(payload["txid"], 3)
        self.assertEqual(payload["items"], 0, "counting keys needs --deep")
        self.assertGreaterEqual(payload["pages"], 3)
        self.assertGreaterEqual(payload["file_pages"], payload["pages"])
        self.assertIn("free_pages", payload)

    def test_an_empty_store_has_no_root(self):
        self.assertEqual(call("stat", self.db)[0], 0)
        _, out, _ = call("stat", self.db)
        self.assertIn("root      empty", out)
        self.assertIn("pages     2", out)


class VerifyTests(CliTestCase):
    def test_verify_reports_a_clean_file(self):
        self.fill(4)
        code, out, err = call("verify", self.db)
        self.assertEqual(code, 0, err)
        self.assertIn("ok: 4 keys", out)
        self.assertIn("depth 1", out)

    def test_verify_json_carries_the_key_range(self):
        for key in ("b", "a", "c"):
            self.put(key, "1")
        _, out, _ = call("verify", self.db, "--json")
        payload = json.loads(out)
        self.assertTrue(payload["ok"])
        self.assertEqual((payload["min_key"], payload["max_key"]), ("a", "c"))
        self.assertEqual(payload["items"], 3)

    def test_verify_catches_a_broken_tree_and_exits_three(self):
        # A branch whose separator is out of order: every page decodes, the
        # root is reachable, and the tree is still wrong.  Only verify() looks
        # far enough to notice.
        disk = FileDisk(self.db)
        disk.write_page(2, pg.encode_branch(1, 3, [(b"z", 4)]))
        disk.write_page(3, pg.encode_leaf(1, [(b"a", b"1")]))
        disk.write_page(4, pg.encode_leaf(1, [(b"b", b"2")]))
        disk.write_page(0, pg.encode_meta(0, 5, 2, 1))
        disk.sync()
        disk.close()

        code, out, _ = call("verify", self.db)
        self.assertEqual(code, cli.EXIT_NO, out)
        self.assertIn("broken at page 4", out)
        self.assertIn("below its range", out)


class FailureTests(CliTestCase):
    def write_garbage(self) -> None:
        with open(self.db, "wb") as handle:
            handle.write(b"this is not a stele file, not even close\n" * 500)

    def test_a_file_that_is_not_a_store_exits_one(self):
        self.write_garbage()
        code, out, err = call("verify", self.db)
        self.assertEqual(code, cli.EXIT_ERROR)
        self.assertIn("refusing to guess", err)
        self.assertEqual(out, "", "the complaint belongs on stderr")

    def test_every_command_refuses_the_same_file(self):
        self.write_garbage()
        for command in (("get", self.db, "k"), ("put", self.db, "k", "v"), ("stat", self.db)):
            with self.subTest(command=command[0]):
                code, _, err = call(*command)
                self.assertEqual(code, cli.EXIT_ERROR)
                self.assertIn("refusing to guess", err)
                self.assertIn(self.db, err, "the message names the file that failed")

    def test_usage_errors_are_two_not_one(self):
        for args in (("get",), ("put", self.db, "k"), ("nonsense",), ("bench", "turbo")):
            with self.subTest(args=args):
                self.assertEqual(call(*args)[0], cli.EXIT_USAGE)


class IntrospectionTests(CliTestCase):
    def test_demo_without_a_name_lists_all_five(self):
        code, out, _ = call("demo")
        self.assertEqual(code, 0)
        for name in ("basic", "snapshot", "crash", "batch", "reclaim"):
            self.assertIn(name, out)

    def test_demo_runs_a_self_checking_script(self):
        code, out, err = call("demo", "basic")
        self.assertEqual(code, 0, err)
        self.assertIn("Basic operations", out)
        self.assertIn("verify: ok", out)

    def test_bench_json_is_a_full_report(self):
        code, out, err = call("bench", "quick", "--json")
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["suite"], "quick")
        self.assertEqual(len(payload["measurements"]), 5)
        self.assertGreater(payload["total_seconds"], 0)
        for item in payload["measurements"]:
            self.assertGreater(item["rows"], 0)
            self.assertIn("amplification", item)

    def test_bench_text_mode_prints_a_table_and_a_footer(self):
        code, out, _ = call("bench", "quick")
        self.assertEqual(code, 0)
        self.assertIn("workload", out)
        self.assertIn("5 workloads", out)
        self.assertIn("page writes", out, "the footprint note follows the table")
        self.assertIn("of logical data", out)


if __name__ == "__main__":
    unittest.main()
