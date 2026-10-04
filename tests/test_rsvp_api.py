import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.app import create_app


class RsvpTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "answers/rsvp.sqlite3"
        self.site_root = Path(temporary.name) / "dist"
        (self.site_root / "assets").mkdir(parents=True)
        (self.site_root / "index.html").write_text(
            "wedding invitation", encoding="utf-8"
        )
        (self.site_root / "assets/photo.txt").write_text("photo", encoding="utf-8")
        self.app = create_app(str(self.database), str(self.site_root))
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()
        self.payload = {
            "name": " Саша Гость ",
            "companion": " Соня, Миша ",
            "attendance": "yes",
            "drink": " Вино ",
            "food": " Без орехов\nВегетарианское меню ",
        }

    def saved_answers(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(
                "SELECT name, companion, attendance, drink, food, created_at FROM rsvps"
            ).fetchall()

    def test_answer_is_committed_with_every_field_and_survives_restart(self):
        response = self.client.post("/api/rsvp", json=self.payload)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json, {"ok": True})
        answers = self.saved_answers()
        self.assertEqual(len(answers), 1)
        self.assertEqual(
            answers[0][:5],
            (
                "Саша Гость",
                "Соня, Миша",
                "yes",
                "Вино",
                "Без орехов\nВегетарианское меню",
            ),
        )
        self.assertRegex(answers[0][5], r"^\d{4}-\d{2}-\d{2}T.*Z$")
        restarted = create_app(str(self.database), str(self.site_root)).test_client()
        self.assertEqual(restarted.get("/api/health").status_code, 200)
        self.assertEqual(self.saved_answers(), answers)

    def test_each_submission_creates_a_separate_answer(self):
        for _ in range(2):
            response = self.client.post("/api/rsvp", json=self.payload)
            self.assertEqual(response.status_code, 201)
        self.assertEqual(len(self.saved_answers()), 2)

    def test_all_attendance_choices_and_omitted_optional_fields(self):
        for attendance in ("yes", "ceremony", "buffet", "banquet", "no"):
            with self.subTest(attendance=attendance):
                response = self.client.post(
                    "/api/rsvp", json={"name": "Гость", "attendance": attendance}
                )
                self.assertEqual(response.status_code, 201)
                self.assertEqual(
                    self.saved_answers()[-1][:5], ("Гость", "", attendance, "", "")
                )

    def test_blank_name_and_invalid_attendance_do_not_save(self):
        for changes in ({"name": " \n "}, {"attendance": "maybe"}, {"attendance": ""}):
            with self.subTest(changes=changes):
                response = self.client.post("/api/rsvp", json=self.payload | changes)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json["ok"])
        self.assertEqual(self.saved_answers(), [])

    def test_non_text_fields_and_unknown_fields_do_not_save(self):
        for field in self.payload:
            with self.subTest(field=field):
                response = self.client.post(
                    "/api/rsvp", json=self.payload | {field: []}
                )
                self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/rsvp", json=self.payload | {"extra": "value"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.saved_answers(), [])

    def test_field_lengths_match_the_form(self):
        for field, limit in (
            ("name", 100),
            ("companion", 200),
            ("drink", 300),
            ("food", 1000),
        ):
            with self.subTest(field=field):
                accepted = self.client.post(
                    "/api/rsvp", json=self.payload | {field: "я" * limit}
                )
                rejected = self.client.post(
                    "/api/rsvp", json=self.payload | {field: "я" * (limit + 1)}
                )
                self.assertEqual(accepted.status_code, 201)
                self.assertEqual(rejected.status_code, 400)
        self.assertEqual(len(self.saved_answers()), 4)

    def test_bad_json_and_non_object_bodies_return_json_errors(self):
        bodies = ("{", "null", "[]", "1", '"text"')
        for body in bodies:
            with self.subTest(body=body):
                response = self.client.post(
                    "/api/rsvp", data=body, content_type="application/json"
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json["ok"])
        self.assertEqual(self.saved_answers(), [])

    def test_only_json_is_accepted_and_large_bodies_are_rejected(self):
        form_response = self.client.post("/api/rsvp", data=self.payload)
        self.assertEqual(form_response.status_code, 415)
        large_response = self.client.post(
            "/api/rsvp",
            data=json.dumps({"name": "x" * 9000}),
            content_type="application/json",
        )
        self.assertEqual(large_response.status_code, 413)
        self.assertFalse(large_response.json["ok"])
        self.assertEqual(self.saved_answers(), [])

    def test_sql_text_is_saved_as_text(self):
        sql_text = "Robert'); DROP TABLE rsvps;--"
        response = self.client.post("/api/rsvp", json=self.payload | {"name": sql_text})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.saved_answers()[0][0], sql_text)

    def test_database_error_does_not_claim_success_or_expose_details(self):
        self.database.unlink()
        self.database.mkdir()
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.client.post("/api/rsvp", json=self.payload)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json,
            {"ok": False, "error": "Ответ не сохранился. Попробуй ещё раз."},
        )
        self.assertNotIn(str(self.database), response.get_data(as_text=True))

    def test_health_checks_database_and_no_answers_are_public(self):
        self.assertEqual(self.client.get("/api/health").json, {"ok": True})
        for path in ("/api/rsvp", "/api/rsvps", "/data/rsvp.sqlite3", "/rsvp.sqlite3"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("DROP TABLE rsvps")
            connection.commit()
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.json["ok"])

    def test_site_index_and_assets_are_served(self):
        with self.client.get("/") as index:
            self.assertEqual(index.status_code, 200)
            self.assertEqual(index.get_data(as_text=True), "wedding invitation")
        with self.client.get("/assets/photo.txt") as asset:
            self.assertEqual(asset.status_code, 200)
            self.assertEqual(asset.get_data(as_text=True), "photo")

    def test_files_outside_site_and_unknown_paths_are_not_served(self):
        private_code = self.site_root.parent / "backend/app.py"
        private_code.parent.mkdir()
        private_code.write_text("private code", encoding="utf-8")
        paths = (
            "/missing.html",
            "/api/missing",
            "/backend/app.py",
            "/answers/rsvp.sqlite3",
            "/../backend/app.py",
            "/%2e%2e/backend/app.py",
            "/assets/../../answers/rsvp.sqlite3",
            "/assets/%2e%2e/%2e%2e/answers/rsvp.sqlite3",
        )
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertFalse(response.json["ok"])


if __name__ == "__main__":
    unittest.main()
