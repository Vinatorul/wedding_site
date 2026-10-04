import csv
import io
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/export-rsvp.py"
HEADER = ["id", "created_at", "name", "companion", "attendance", "drink", "food"]


class ExportRsvpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "rsvp.sqlite3"
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "CREATE TABLE rsvps (id INTEGER PRIMARY KEY, name TEXT, companion TEXT, "
                "attendance TEXT, drink TEXT, food TEXT, created_at TEXT)"
            )
            connection.commit()

    def seed_answers(self, rows):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executemany(
                "INSERT INTO rsvps (created_at, name, companion, attendance, drink, food) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            connection.commit()

    def run_export(self, database=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--database", str(database or self.database)],
            capture_output=True,
            timeout=10,
        )

    def parsed_csv(self, result):
        return list(
            csv.reader(io.StringIO(result.stdout.decode("utf-8-sig"), newline=""))
        )

    def test_cli_csv_preserves_russian_quotes_commas_and_multiline_fields(self):
        rows = [
            (
                "2026-10-04T12:34:56.000Z",
                'Гость "Рома"',
                "Соня, Миша",
                "yes",
                "Красное вино",
                'Без орехов\nМожно "рыбу"',
            ),
            ("2026-10-04T12:35:56.000Z", "Соня", "", "no", "", ""),
        ]
        self.seed_answers(rows)
        result = self.run_export()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, b"")
        self.assertTrue(result.stdout.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(
            self.parsed_csv(result), [HEADER, ["1", *rows[0]], ["2", *rows[1]]]
        )

    def test_empty_table_exports_only_header_and_bom(self):
        result = self.run_export()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout, b"\xef\xbb\xbf" + ",".join(HEADER).encode() + b"\r\n"
        )
        self.assertEqual(self.parsed_csv(result), [HEADER])

    def test_missing_database_fails_without_creating_file_or_csv(self):
        missing = self.database.parent / "missing.sqlite3"
        result = self.run_export(missing)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertIn("Не удалось выгрузить ответы", result.stderr.decode("utf-8"))
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertFalse(missing.exists())

    def test_uninitialized_database_fails_before_writing_csv(self):
        empty = self.database.parent / "empty.sqlite3"
        with closing(sqlite3.connect(empty)):
            pass
        original = empty.read_bytes()
        result = self.run_export(empty)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertIn("Не удалось выгрузить ответы", result.stderr.decode("utf-8"))
        self.assertEqual(empty.read_bytes(), original)

    def test_corrupt_database_fails_without_csv_or_changes(self):
        self.database.write_bytes(b"not a SQLite database")
        original = self.database.read_bytes()
        result = self.run_export()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertNotIn(b"Traceback", result.stderr)
        self.assertEqual(self.database.read_bytes(), original)

    def test_readonly_database_contents_schema_and_permissions_are_unchanged(self):
        self.seed_answers([("2026-10-04T12:34:56.000Z", "Гость", "", "buffet", "", "")])
        self.database.chmod(0o444)
        original = self.database.read_bytes()
        metadata = self.database.stat()
        files = set(self.database.parent.iterdir())
        result = self.run_export()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.database.read_bytes(), original)
        self.assertEqual(self.database.stat().st_mode, metadata.st_mode)
        self.assertEqual(self.database.stat().st_mtime_ns, metadata.st_mtime_ns)
        self.assertEqual(set(self.database.parent.iterdir()), files)
        self.assertEqual(self.parsed_csv(result)[1][4], "buffet")


if __name__ == "__main__":
    unittest.main()
