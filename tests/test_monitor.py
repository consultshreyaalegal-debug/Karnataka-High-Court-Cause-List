import unittest
from pathlib import Path

from monitor import html_text, row_records, tables_from_html


class MonitorParsingTests(unittest.TestCase):
    def test_local_fixture_generates_match(self):
        html = Path("fixtures/local_result.html").read_text(encoding="utf-8")
        lines = html_text(html).splitlines()
        tables = tables_from_html(html)

        self.assertEqual(len(tables), 1)

        confirmed, uncertain = row_records(tables[0], lines, "Bengaluru Bench", "local-test")

        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["Case Number"], "WP 12345/2026")
        self.assertEqual(confirmed[0]["Advocate Name & Variant Matched"].startswith("Mahesh Choudhary"), True)
        self.assertEqual(len(uncertain), 0)


if __name__ == "__main__":
    unittest.main()
