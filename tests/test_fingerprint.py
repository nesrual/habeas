import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import fingerprint  # noqa: E402

SNIPPET = """
def fee_cents(amount_cents)
  dollars = amount_cents / 100.0
  (dollars * RATE * 100).to_i + FIXED_CENTS
end
"""


class TestNormalize(unittest.TestCase):
    def test_strips_and_collapses_whitespace(self):
        self.assertEqual(fingerprint.normalize("  a   =  1  \n\tb = 2\t"), "a = 1\nb = 2")

    def test_drops_comment_only_and_blank_lines(self):
        self.assertEqual(
            fingerprint.normalize("# a comment\n\na = 1\n   # indented comment\n"),
            "a = 1",
        )

    def test_keeps_trailing_comments_on_code_lines(self):
        self.assertEqual(fingerprint.normalize("a = 1  # why"), "a = 1 # why")


class TestCompute(unittest.TestCase):
    def test_shape(self):
        fp = fingerprint.compute("lib/fees.rb", "bug", SNIPPET)
        self.assertRegex(fp, r"^[0-9a-f]{16}$")

    def test_survives_reformatting(self):
        reformatted = SNIPPET.replace("  dollars", "        dollars").replace("\n", "\n\n")
        commented = "# reviewed 2026-09\n" + SNIPPET
        base = fingerprint.compute("lib/fees.rb", "bug", SNIPPET)
        self.assertEqual(base, fingerprint.compute("lib/fees.rb", "bug", reformatted))
        self.assertEqual(base, fingerprint.compute("lib/fees.rb", "bug", commented))

    def test_sensitive_to_path_category_and_code(self):
        base = fingerprint.compute("lib/fees.rb", "bug", SNIPPET)
        self.assertNotEqual(base, fingerprint.compute("lib/other.rb", "bug", SNIPPET))
        self.assertNotEqual(base, fingerprint.compute("lib/fees.rb", "security", SNIPPET))
        self.assertNotEqual(base, fingerprint.compute("lib/fees.rb", "bug", SNIPPET.replace("100.0", "100")))


if __name__ == "__main__":
    unittest.main()
