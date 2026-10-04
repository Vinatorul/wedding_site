#!/usr/bin/env python3
"""Выгрузить ответы на приглашение в CSV с кодировкой UTF-8 для Excel."""

import argparse
import csv
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

COLUMNS = ("id", "created_at", "name", "companion", "attendance", "drink", "food")
QUERY = "SELECT id, created_at, name, companion, attendance, drink, food FROM rsvps ORDER BY id"


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("/srv/wedding-site/data/rsvp.sqlite3"),
        help="Путь к SQLite с ответами (по умолчанию: %(default)s)",
    )
    return parser.parse_args(argv)


def export_csv(database, output):
    uri = database.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        rows = connection.execute(QUERY)
        output.write("\ufeff")
        writer = csv.writer(output)
        writer.writerow(COLUMNS)
        writer.writerows(rows)


def main(argv=None):
    args = arguments(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", newline="")
        export_csv(args.database, sys.stdout)
    except (sqlite3.Error, OSError, UnicodeError) as error:
        print(f"Не удалось выгрузить ответы: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
