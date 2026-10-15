import json
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

COOKIE_NAME = "wedding_tasks"
COOKIE_AGE = 365 * 24 * 60 * 60
SCHEMAS = (
    """
    CREATE TABLE IF NOT EXISTS task_participants (
        token TEXT PRIMARY KEY NOT NULL,
        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS participant_tasks (
        id INTEGER PRIMARY KEY,
        participant_token TEXT NOT NULL REFERENCES task_participants(token),
        position INTEGER NOT NULL,
        text TEXT NOT NULL,
        completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
        UNIQUE (participant_token, position)
    )
    """,
)
blueprint = Blueprint("tasks", __name__)


def initialize_tasks_database(connection):
    for schema in SCHEMAS:
        connection.execute(schema)


def load_catalog(catalog):
    if catalog is None:
        path = Path(__file__).with_name("task_catalog.json")
        catalog = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(catalog, dict) or set(catalog) != {"common", "random_groups"}:
        raise ValueError("Task catalog must contain common and random_groups lists")
    common, groups = catalog["common"], catalog["random_groups"]
    if not isinstance(common, list) or not isinstance(groups, list):
        raise ValueError("Task catalog must contain lists")
    if any(not isinstance(group, list) or not group for group in groups):
        raise ValueError("Random task groups must be nonempty lists")
    texts = common + [text for group in groups for text in group]
    if any(not isinstance(text, str) or not text.strip() for text in texts):
        raise ValueError("Tasks must contain nonempty text")
    return {
        "common": common.copy(),
        "random_groups": [group.copy() for group in groups],
    }


def connect_database():
    connection = sqlite3.connect(current_app.config["DATABASE"], timeout=5)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def task_state(connection, token):
    assigned = connection.execute(
        "SELECT 1 FROM task_participants WHERE token = ?", (token,)
    ).fetchone()
    rows = connection.execute(
        "SELECT id, text, completed FROM participant_tasks "
        "WHERE participant_token = ? ORDER BY position",
        (token,),
    ).fetchall()
    catalog = current_app.config["TASK_CATALOG"]
    return {
        "ok": True,
        "assigned": bool(assigned),
        "available": bool(catalog["common"] or catalog["random_groups"]),
        "tasks": [
            {"id": task_id, "text": text, "completed": bool(completed)}
            for task_id, text, completed in rows
        ],
    }


def json_object():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("Проверь формат задания.")
    return payload


def select_tasks():
    catalog = current_app.config["TASK_CATALOG"]
    return catalog["common"] + [
        secrets.choice(group) for group in catalog["random_groups"]
    ]


def assignment_response(state, token):
    response = jsonify(state)
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=COOKIE_AGE,
        httponly=True,
        secure=request.is_secure or request.headers.get("X-Forwarded-Proto") == "https",
        samesite="Lax",
    )
    return response, 201


@blueprint.get("/api/tasks")
def get_tasks():
    with closing(connect_database()) as connection:
        return jsonify(task_state(connection, request.cookies.get(COOKIE_NAME)))


@blueprint.post("/api/tasks")
def assign_tasks():
    if json_object():
        raise ValueError("Проверь формат задания.")
    token = request.cookies.get(COOKIE_NAME)
    with closing(connect_database()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        state = task_state(connection, token)
        if state["assigned"]:
            return jsonify(state), 200
        texts = select_tasks()
        if not texts:
            return jsonify(
                ok=False, error="Задания скоро появятся. Загляни чуть позже."
            ), 409
        token = secrets.token_urlsafe(32)
        connection.execute("INSERT INTO task_participants (token) VALUES (?)", (token,))
        connection.executemany(
            "INSERT INTO participant_tasks (participant_token, position, text) "
            "VALUES (?, ?, ?)",
            [(token, position, text) for position, text in enumerate(texts)],
        )
        state = task_state(connection, token)
    return assignment_response(state, token)


@blueprint.patch("/api/tasks/<int:task_id>")
def update_task(task_id):
    payload = json_object()
    if set(payload) != {"completed"} or type(payload["completed"]) is not bool:
        raise ValueError("Укажи, выполнено ли задание.")
    token = request.cookies.get(COOKIE_NAME)
    with closing(connect_database()) as connection, connection:
        updated = connection.execute(
            "UPDATE participant_tasks SET completed = ? "
            "WHERE id = ? AND participant_token = ?",
            (payload["completed"], task_id, token),
        )
        if updated.rowcount != 1:
            return jsonify(ok=False, error="Задание не найдено."), 404
        return jsonify(task_state(connection, token))


@blueprint.get("/tasks")
@blueprint.get("/tasks/")
def tasks_page():
    return current_app.send_static_file("tasks/index.html")


@blueprint.errorhandler(ValueError)
def invalid_request(error):
    return jsonify(ok=False, error=str(error)), 400


@blueprint.errorhandler(sqlite3.Error)
def database_error(error):
    current_app.logger.exception("Tasks database unavailable")
    return jsonify(
        ok=False, error="Задания временно недоступны. Попробуй ещё раз."
    ), 503


def no_store_api(response):
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def register_tasks(app, catalog):
    app.config["TASK_CATALOG"] = load_catalog(catalog)
    app.register_blueprint(blueprint)
    app.after_request(no_store_api)
