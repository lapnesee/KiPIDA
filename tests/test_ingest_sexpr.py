"""Tests for ingest.sexpr — S-expression parser."""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.sexpr import parse, find, find_all, get_str


class TestParse(unittest.TestCase):
    def test_simple_list(self):
        result = parse("(foo bar baz)")
        self.assertEqual(result, [["foo", "bar", "baz"]])

    def test_nested(self):
        result = parse("(a (b c) (d e f))")
        self.assertEqual(result, [["a", ["b", "c"], ["d", "e", "f"]]])

    def test_quoted_string(self):
        result = parse('(key "hello world")')
        self.assertEqual(result, [["key", "hello world"]])

    def test_escaped_quote_in_string(self):
        result = parse('(key "say \\"hi\\"")')
        self.assertEqual(result, [['key', 'say "hi"']])

    def test_multiple_top_level(self):
        result = parse("(a 1) (b 2)")
        self.assertEqual(result, [["a", "1"], ["b", "2"]])

    def test_empty(self):
        result = parse("")
        self.assertEqual(result, [])

    def test_kicad_like(self):
        text = '(kicad_sch (version 20260306) (uuid "abc-123"))'
        result = parse(text)
        self.assertEqual(result[0][0], "kicad_sch")
        self.assertEqual(result[0][1], ["version", "20260306"])
        self.assertEqual(result[0][2], ["uuid", "abc-123"])

    def test_line_comment_skipped(self):
        result = parse("; this is a comment\n(foo bar)")
        self.assertEqual(result, [["foo", "bar"]])

    def test_numeric_atoms(self):
        result = parse("(at 39.37 26.67)")
        self.assertEqual(result, [["at", "39.37", "26.67"]])

    def test_deep_nesting(self):
        result = parse("(a (b (c (d e))))")
        self.assertEqual(result[0], ["a", ["b", ["c", ["d", "e"]]]])


class TestFind(unittest.TestCase):
    def setUp(self):
        self.node = parse("(root (name foo) (value bar) (value baz))")[0]

    def test_find_present(self):
        child = find(self.node, "name")
        self.assertIsNotNone(child)
        self.assertEqual(child[0], "name")
        self.assertEqual(child[1], "foo")

    def test_find_missing(self):
        self.assertIsNone(find(self.node, "missing"))

    def test_find_returns_first(self):
        child = find(self.node, "value")
        self.assertEqual(child[1], "bar")

    def test_find_non_list(self):
        self.assertIsNone(find("not a list", "x"))


class TestFindAll(unittest.TestCase):
    def setUp(self):
        self.node = parse("(root (pin 1) (pin 2) (pin 3) (other x))")[0]

    def test_find_all_multiple(self):
        pins = find_all(self.node, "pin")
        self.assertEqual(len(pins), 3)
        self.assertEqual(pins[0][1], "1")
        self.assertEqual(pins[2][1], "3")

    def test_find_all_none(self):
        self.assertEqual(find_all(self.node, "absent"), [])

    def test_find_all_non_list(self):
        self.assertEqual(find_all("nope", "x"), [])


class TestGetStr(unittest.TestCase):
    def setUp(self):
        self.node = parse('(module_block (name "P02 Alimentation") (uuid "xyz"))')[0]

    def test_get_str_present(self):
        self.assertEqual(get_str(self.node, "name"), "P02 Alimentation")

    def test_get_str_missing_default(self):
        self.assertEqual(get_str(self.node, "missing", "fallback"), "fallback")

    def test_get_str_empty_default(self):
        self.assertEqual(get_str(self.node, "missing"), "")

    def test_get_str_uuid(self):
        self.assertEqual(get_str(self.node, "uuid"), "xyz")


if __name__ == "__main__":
    unittest.main()
