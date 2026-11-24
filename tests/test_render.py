"""Terminal rendering: widths, alignment, and the numbers people read."""

from __future__ import annotations

import unittest

from stele import render


class WidthTests(unittest.TestCase):
    def test_ascii_counts_one_per_character(self):
        self.assertEqual(render.display_width(""), 0)
        self.assertEqual(render.display_width("key-000123"), 10)

    def test_cjk_counts_two_cells(self):
        self.assertEqual(render.display_width("杭州"), 4)
        self.assertEqual(render.display_width("键 value"), 8)

    def test_combining_marks_count_nothing(self):
        self.assertEqual(render.display_width("e\u0301"), 1, "e + combining acute")
        self.assertEqual(render.display_width("a\u0300\u0301b"), 2)

    def test_fullwidth_forms_count_two(self):
        self.assertEqual(render.display_width("ｆｕｌｌ"), 8)

    def test_pad_respects_display_width(self):
        self.assertEqual(render.pad("杭", 4), "杭  ")
        self.assertEqual(render.pad("1", 3, "right"), "  1")
        self.assertEqual(render.pad("杭州", 4), "杭州")
        self.assertEqual(render.pad("杭州", 2), "杭州", "a wide cell is never cut")

    def test_pad_never_pads_on_the_right_for_right_alignment(self):
        self.assertEqual(render.pad("abc", 5, "left"), "abc  ")
        self.assertEqual(render.pad("abc", 5, "right"), "  abc")
        self.assertEqual(render.pad("abc", 3), "abc")


class TableTests(unittest.TestCase):
    def test_columns_line_up_across_cjk_and_ascii(self):
        text = render.table(["key", "值"], [["杭州", "1"], ["a", "22"]])
        lines = text.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0], "key   " + "值")
        self.assertEqual(lines[2], "杭州  1")
        self.assertEqual(lines[3], "a     22", "the second column starts at one column")

    def test_separator_matches_each_column_width(self):
        dash = render.glyph("dash")
        text = render.table(["key", "值"], [["杭州", "1"]])
        separator = text.splitlines()[1]
        self.assertEqual(separator, dash * 4 + "  " + dash * 2)

    def test_right_alignment_pads_on_the_left(self):
        text = render.table(["n", "size"], [["1", "4 B"], ["22", "1234 B"]], aligns=["right", "right"])
        lines = text.splitlines()
        self.assertEqual(lines[2], " 1     4 B")
        self.assertEqual(lines[3], "22  1234 B")

    def test_headers_alone_are_enough(self):
        text = render.table(["only"], [])
        self.assertEqual(len(text.splitlines()), 2)

    def test_trailing_spaces_are_trimmed(self):
        text = render.table(["a", "b"], [["1", ""]])
        for line in text.splitlines():
            self.assertEqual(line, line.rstrip(), "no line may end in whitespace")


class NumberTests(unittest.TestCase):
    def test_thousands_get_a_separator(self):
        self.assertEqual(render.number(1234.4), "1,234")
        self.assertEqual(render.number(1_939_488.0), "1,939,488")

    def test_middle_range_keeps_one_decimal(self):
        self.assertEqual(render.number(12.34), "12.3")
        self.assertEqual(render.number(99.0), "99")
        self.assertEqual(render.number(10.5), "10.5")

    def test_small_numbers_keep_three_significant_digits(self):
        self.assertEqual(render.number(5.0), "5")
        self.assertEqual(render.number(0.125), "0.125")

    def test_seconds_pick_a_unit(self):
        self.assertEqual(render.seconds(0.0005), "500 µs")
        self.assertEqual(render.seconds(0.25), "250.0 ms")
        self.assertEqual(render.seconds(2.5), "2.50 s")

    def test_sizes_pick_a_unit(self):
        self.assertEqual(render.size(900), "900 B")
        self.assertEqual(render.size(4096), "4.0 KiB")
        self.assertEqual(render.size(1024 * 1024), "1.00 MiB")
        self.assertEqual(render.size(5 * 1024 * 1024 + 512 * 1024), "5.50 MiB")

    def test_bar_fills_proportionally_and_clamps(self):
        bar = render.glyph("bar")
        self.assertEqual(render.bar(0.5, 10), bar * 5 + " " * 5)
        self.assertEqual(render.bar(-3, 10), " " * 10)
        self.assertEqual(render.bar(9, 10), bar * 10)
        self.assertEqual(len(render.bar(0.33, 24)), 24)

    def test_plural_picks_the_right_word(self):
        self.assertEqual(render.plural(0, "key"), "0 keys")
        self.assertEqual(render.plural(1, "key"), "1 key")
        self.assertEqual(render.plural(1234, "key"), "1,234 keys")
        self.assertEqual(render.plural(1, "leaf", "leaves"), "1 leaf")
        self.assertEqual(render.plural(2, "leaf", "leaves"), "2 leaves")

    def test_glyph_comes_from_the_current_encoding(self):
        self.assertIn(render.glyph("dash"), set(render.GLYPHS["ascii"].values()) | set(render.GLYPHS["utf8"].values()))


class WrapTests(unittest.TestCase):
    def test_lines_fit_the_requested_width(self):
        lines = render.wrap("the quick brown fox jumps over the lazy dog", 20)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(render.display_width(line), 20)

    def test_words_are_never_split(self):
        lines = render.wrap("alpha beta gamma", 11)
        self.assertEqual(lines, ["alpha beta", "gamma"])

    def test_indent_applies_to_every_line(self):
        lines = render.wrap("a b c", 3, indent="  ")
        self.assertEqual(lines, ["  a", "  b", "  c"])

    def test_cjk_is_measured_in_cells(self):
        lines = render.wrap("杭州 南京 北京", 6)
        self.assertEqual(lines, ["杭州", "南京", "北京"])

    def test_empty_text_gives_no_lines(self):
        self.assertEqual(render.wrap("", 10), [])


if __name__ == "__main__":
    unittest.main()
