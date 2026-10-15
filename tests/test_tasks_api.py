import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backend.app import create_app
from backend.tasks import COOKIE_AGE, COOKIE_NAME

CATALOG = {
    "common": ["Общее задание 1", "Общее задание 2"],
    "random_groups": [["Вариант 1А", "Вариант 1Б"], ["Вариант 2А", "Вариант 2Б"]],
}


class TasksTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "answers/rsvp.sqlite3"
        self.site_root = Path(temporary.name) / "dist"
        (self.site_root / "tasks").mkdir(parents=True)
        (self.site_root / "index.html").write_text("invitation", encoding="utf-8")
        (self.site_root / "tasks/index.html").write_text("tasks page", encoding="utf-8")
        self.app = self.make_app(CATALOG)
        self.client = self.app.test_client()

    def make_app(self, catalog):
        app = create_app(str(self.database), str(self.site_root), catalog)
        app.config["TESTING"] = True
        return app

    def participant_count(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(
                "SELECT count(*) FROM task_participants"
            ).fetchone()[0]

    def restart_client(self, catalog):
        client = self.make_app(catalog).test_client()
        client.set_cookie(COOKIE_NAME, self.client.get_cookie(COOKIE_NAME).value)
        return client

    def test_get_does_not_allocate_or_set_cookie(self):
        response = self.client.get("/api/tasks")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json,
            {"ok": True, "assigned": False, "available": True, "tasks": []},
        )
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(self.participant_count(), 0)

    def test_assignment_contains_common_tasks_and_one_choice_per_group(self):
        response = self.client.post("/api/tasks", json={})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json["assigned"])
        tasks = response.json["tasks"]
        self.assertEqual(len(tasks), 4)
        self.assertEqual([task["text"] for task in tasks[:2]], CATALOG["common"])
        for task, group in zip(tasks[2:], CATALOG["random_groups"]):
            self.assertIn(task["text"], group)
        self.assertTrue(all(type(task["id"]) is int for task in tasks))
        self.assertTrue(all(task["completed"] is False for task in tasks))
        self.assertEqual(self.participant_count(), 1)

    def test_repeated_assignment_and_reload_return_the_saved_set(self):
        initial = self.client.post("/api/tasks", json={}).json
        repeated = self.client.post("/api/tasks", json={})
        self.assertEqual(repeated.status_code, 200)
        self.assertEqual(repeated.json, initial)
        self.assertEqual(self.client.get("/api/tasks").json, initial)
        self.assertEqual(self.participant_count(), 1)

    def test_saved_text_and_random_choices_survive_restart_and_catalog_changes(self):
        initial = self.client.post("/api/tasks", json={}).json
        changed_catalog = {"common": ["Новые задания"], "random_groups": []}
        restarted = self.restart_client(changed_catalog)
        self.assertEqual(restarted.get("/api/tasks").json, initial)
        self.assertEqual(restarted.post("/api/tasks", json={}).json, initial)
        self.assertEqual(self.participant_count(), 1)

    def test_mark_and_unmark_survive_reload_and_restart(self):
        task_id = self.client.post("/api/tasks", json={}).json["tasks"][0]["id"]
        path = f"/api/tasks/{task_id}"
        marked = self.client.patch(path, json={"completed": True})
        self.assertEqual(marked.status_code, 200)
        self.assertTrue(marked.json["tasks"][0]["completed"])
        self.assertEqual(self.client.get("/api/tasks").json, marked.json)
        restarted = self.restart_client(CATALOG)
        self.assertEqual(restarted.get("/api/tasks").json, marked.json)
        unmarked = restarted.patch(path, json={"completed": False})
        self.assertEqual(unmarked.status_code, 200)
        self.assertFalse(unmarked.json["tasks"][0]["completed"])
        self.assertEqual(self.client.get("/api/tasks").json, unmarked.json)

    def test_other_participants_and_unknown_ids_cannot_be_changed(self):
        own = self.client.post("/api/tasks", json={}).json
        other = self.app.test_client()
        other_state = other.post("/api/tasks", json={}).json
        self.assertNotEqual(own["tasks"][0]["id"], other_state["tasks"][0]["id"])
        task_id = own["tasks"][0]["id"]
        for client, path in (
            (other, f"/api/tasks/{task_id}"),
            (self.app.test_client(), f"/api/tasks/{task_id}"),
            (self.client, "/api/tasks/99999"),
        ):
            with self.subTest(path=path):
                response = client.patch(path, json={"completed": True})
                self.assertEqual(response.status_code, 404)
                self.assertFalse(response.json["ok"])
        self.assertEqual(self.client.get("/api/tasks").json, own)
        self.assertEqual(other.get("/api/tasks").json, other_state)

    def test_cookie_is_unpredictable_persistent_and_not_readable_by_javascript(self):
        response = self.client.post("/api/tasks", json={})
        cookie = response.headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        self.assertIn(f"Max-Age={COOKIE_AGE}", cookie)
        self.assertIn("Path=/", cookie)
        self.assertNotIn("Secure", cookie)
        token = self.client.get_cookie(COOKIE_NAME).value
        self.assertRegex(token, r"^[A-Za-z0-9_-]{43}$")
        second = self.app.test_client()
        second.post("/api/tasks", json={})
        self.assertNotEqual(token, second.get_cookie(COOKIE_NAME).value)

    def test_cookie_is_secure_on_direct_and_forwarded_https(self):
        requests = (
            {"base_url": "https://example.test"},
            {"headers": {"X-Forwarded-Proto": "https"}},
        )
        for options in requests:
            with self.subTest(options=options):
                response = self.app.test_client().post("/api/tasks", json={}, **options)
                self.assertIn("Secure", response.headers["Set-Cookie"])

    def test_unknown_cookie_is_replaced_only_on_successful_assignment(self):
        self.client.set_cookie(COOKIE_NAME, "unknown-token")
        response = self.client.get("/api/tasks")
        self.assertFalse(response.json["assigned"])
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(self.client.get_cookie(COOKIE_NAME).value, "unknown-token")
        assigned = self.client.post("/api/tasks", json={})
        self.assertEqual(assigned.status_code, 201)
        self.assertNotEqual(self.client.get_cookie(COOKIE_NAME).value, "unknown-token")
        self.assertEqual(self.participant_count(), 1)

    def test_empty_catalog_does_not_allocate_an_empty_set(self):
        empty = self.make_app({"common": [], "random_groups": []}).test_client()
        empty.set_cookie(COOKIE_NAME, "unknown-token")
        state = empty.get("/api/tasks").json
        self.assertFalse(state["assigned"])
        self.assertFalse(state["available"])
        response = empty.post("/api/tasks", json={})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json,
            {"ok": False, "error": "Задания скоро появятся. Загляни чуть позже."},
        )
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(empty.get_cookie(COOKIE_NAME).value, "unknown-token")
        self.assertEqual(self.participant_count(), 0)

    def test_existing_tasks_remain_available_after_catalog_is_emptied(self):
        initial = self.client.post("/api/tasks", json={}).json
        initial["available"] = False
        restarted = self.restart_client({"common": [], "random_groups": []})
        self.assertEqual(restarted.get("/api/tasks").json, initial)
        response = restarted.post("/api/tasks", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, initial)

    def test_assignment_rejects_nonempty_and_invalid_json(self):
        for body in ("{", "null", "[]", "1", '"text"', '{"name":"guest"}'):
            with self.subTest(body=body):
                response = self.client.post(
                    "/api/tasks", data=body, content_type="application/json"
                )
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json["ok"])
        self.assertEqual(self.client.post("/api/tasks", data={}).status_code, 400)
        self.assertEqual(self.participant_count(), 0)

    def test_completion_requires_only_a_boolean_value(self):
        initial = self.client.post("/api/tasks", json={}).json
        path = f"/api/tasks/{initial['tasks'][0]['id']}"
        invalid = ({}, {"completed": 1}, {"completed": "true"}, {"completed": None})
        for payload in invalid + ({"completed": True, "text": "changed"},):
            with self.subTest(payload=payload):
                response = self.client.patch(path, json=payload)
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json["ok"])
        self.assertEqual(
            self.client.patch(
                path, data="{", content_type="application/json"
            ).status_code,
            400,
        )
        self.assertEqual(self.client.get("/api/tasks").json, initial)

    def test_database_failure_returns_honest_json_for_all_task_operations(self):
        task_id = self.client.post("/api/tasks", json={}).json["tasks"][0]["id"]
        self.database.unlink()
        self.database.mkdir()
        operations = (
            ("get", "/api/tasks", None),
            ("post", "/api/tasks", {}),
            ("patch", f"/api/tasks/{task_id}", {"completed": True}),
        )
        for method, path, payload in operations:
            with (
                self.subTest(method=method),
                self.assertLogs(self.app.logger, level="ERROR"),
            ):
                response = getattr(self.client, method)(path, json=payload)
                self.assertEqual(response.status_code, 503)
                self.assertFalse(response.json["ok"])
                self.assertNotIn(str(self.database), response.get_data(as_text=True))

    def test_assignment_failure_rolls_back_participant_and_partial_tasks(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_task BEFORE INSERT ON participant_tasks "
                "WHEN NEW.text = 'Общее задание 2' "
                "BEGIN SELECT RAISE(ABORT, 'storage failure'); END"
            )
        with self.assertLogs(self.app.logger, level="ERROR"):
            response = self.client.post("/api/tasks", json={})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(self.participant_count(), 0)
        with closing(sqlite3.connect(self.database)) as connection:
            tasks = connection.execute(
                "SELECT count(*) FROM participant_tasks"
            ).fetchone()
        self.assertEqual(tasks, (0,))

    def test_tasks_routes_serve_the_separate_page(self):
        for path in ("/tasks", "/tasks/"):
            with self.subTest(path=path), self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_data(as_text=True), "tasks page")
        with self.client.get("/") as response:
            self.assertEqual(response.get_data(as_text=True), "invitation")

    def test_all_api_responses_are_not_cached_and_catalog_is_private(self):
        task_id = self.client.post("/api/tasks", json={}).json["tasks"][0]["id"]
        responses = (
            self.client.get("/api/tasks"),
            self.client.post("/api/tasks", json={}),
            self.client.patch(f"/api/tasks/{task_id}", json={"completed": True}),
            self.client.post("/api/tasks", json={"unexpected": True}),
            self.client.get("/api/tasks/catalog"),
            self.client.get("/api/health"),
        )
        for response in responses:
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(responses[4].status_code, 404)
        self.assertNotIn("random_groups", responses[0].json)

    def test_schema_addition_preserves_existing_rsvp_answers(self):
        payload = {"name": "Гость", "attendance": "yes"}
        self.assertEqual(self.client.post("/api/rsvp", json=payload).status_code, 201)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("DROP TABLE participant_tasks")
            connection.execute("DROP TABLE task_participants")
        self.make_app(CATALOG)
        self.client.post("/api/tasks", json={})
        with closing(sqlite3.connect(self.database)) as connection:
            rows = connection.execute("SELECT name, attendance FROM rsvps").fetchall()
        self.assertEqual(rows, [("Гость", "yes")])

    def test_only_common_and_only_random_catalogs_can_be_assigned(self):
        catalogs = (
            {"common": ["Общее"], "random_groups": []},
            {"common": [], "random_groups": [["Вариант А", "Вариант Б"]]},
        )
        for catalog in catalogs:
            with self.subTest(catalog=catalog):
                client = self.make_app(catalog).test_client()
                response = client.post("/api/tasks", json={})
                self.assertEqual(response.status_code, 201)
                self.assertEqual(len(response.json["tasks"]), 1)


if __name__ == "__main__":
    unittest.main()
