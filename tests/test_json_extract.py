import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import json_extract  # noqa: E402


class TestExtract(unittest.TestCase):
    def test_bare_object(self):
        self.assertEqual(json_extract.extract('{"a": 1}'), {"a": 1})

    def test_bare_array(self):
        self.assertEqual(json_extract.extract("[1, 2, 3]"), [1, 2, 3])

    def test_fenced(self):
        text = 'Here you go:\n```json\n{"verdict": "kill"}\n```\nHope that helps!'
        self.assertEqual(json_extract.extract(text), {"verdict": "kill"})

    def test_prose_before_and_after(self):
        text = 'I analyzed the code. The result is {"ok": true} as requested.'
        self.assertEqual(json_extract.extract(text), {"ok": True})

    def test_skips_broken_json_before_valid(self):
        text = "{not json at all} then real: [1, 2]"
        self.assertEqual(json_extract.extract(text), [1, 2])

    def test_nested_braces_in_strings(self):
        text = 'x {"cmd": "run {file} now", "n": [1, {"k": "}"}]} y'
        self.assertEqual(
            json_extract.extract(text),
            {"cmd": "run {file} now", "n": [1, {"k": "}"}]},
        )

    def test_first_valid_value_wins(self):
        self.assertEqual(json_extract.extract('{"first": 1} {"second": 2}'), {"first": 1})

    def test_no_json_raises(self):
        with self.assertRaises(ValueError):
            json_extract.extract("nothing here, sorry")

    def test_bare_scalars_ignored(self):
        with self.assertRaises(ValueError):
            json_extract.extract("the answer is 42, or true")


if __name__ == "__main__":
    unittest.main()
