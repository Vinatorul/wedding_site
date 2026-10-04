import os
import sqlite3
from contextlib import closing
from pathlib import Path

from flask import Flask, current_app, jsonify, request
from werkzeug.exceptions import HTTPException

FIELD_LIMITS = {
    "name": 100,
    "companion": 200,
    "attendance": 8,
    "drink": 300,
    "food": 1000,
}
ATTENDANCE = {"yes", "ceremony", "buffet", "banquet", "no"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS rsvps (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    companion TEXT NOT NULL,
    attendance TEXT NOT NULL CHECK (
        attendance IN ('yes', 'ceremony', 'buffet', 'banquet', 'no')
    ),
    drink TEXT NOT NULL,
    food TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""


def initialize_database(database):
    Path(database).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(database, timeout=5)) as connection:
        with connection:
            connection.execute(SCHEMA)


def validate_rsvp(payload):
    if not isinstance(payload, dict) or set(payload) - FIELD_LIMITS.keys():
        raise ValueError("Проверь формат анкеты.")
    values = {}
    for field, limit in FIELD_LIMITS.items():
        value = payload.get(field, "")
        if not isinstance(value, str):
            raise ValueError("Все поля анкеты должны содержать текст.")
        if len(value) > limit:
            raise ValueError("Сократи текст в полях анкеты.")
        values[field] = value.strip()
    if not values["name"]:
        raise ValueError("Напиши своё имя.")
    if values["attendance"] not in ATTENDANCE:
        raise ValueError("Выбери, получится ли прийти.")
    return values


def submit_rsvp():
    try:
        values = validate_rsvp(request.get_json())
    except ValueError as error:
        return jsonify(ok=False, error=str(error)), 400
    try:
        with closing(sqlite3.connect(current_app.config["DATABASE"], timeout=5)) as db:
            with db:
                db.execute(
                    "INSERT INTO rsvps (name, companion, attendance, drink, food) "
                    "VALUES (?, ?, ?, ?, ?)",
                    tuple(values[field] for field in FIELD_LIMITS),
                )
    except sqlite3.Error:
        current_app.logger.exception("Could not save RSVP")
        return jsonify(ok=False, error="Ответ не сохранился. Попробуй ещё раз."), 503
    return jsonify(ok=True), 201


def health():
    try:
        with closing(sqlite3.connect(current_app.config["DATABASE"], timeout=5)) as db:
            db.execute("SELECT 1 FROM rsvps LIMIT 1")
    except sqlite3.Error:
        current_app.logger.exception("RSVP database unavailable")
        return jsonify(ok=False, error="Сервис временно недоступен."), 503
    return jsonify(ok=True)


def index():
    return current_app.send_static_file("index.html")


def handle_http_error(error):
    messages = {
        400: "Проверь формат анкеты.",
        413: "Ответ слишком большой. Сократи текст и попробуй ещё раз.",
        415: "Отправь анкету в формате JSON.",
        500: "Ответ не сохранился. Попробуй ещё раз.",
    }
    return jsonify(
        ok=False, error=messages.get(error.code, "Запрос не удалось обработать.")
    ), error.code


def create_app(database_path=None, site_root=None):
    default_site = Path(__file__).resolve().parents[1] / "dist"
    site_root = site_root or os.environ.get("SITE_ROOT", str(default_site))
    app = Flask(__name__, static_folder=site_root, static_url_path="")
    default_path = Path(__file__).resolve().parent / "data" / "rsvp.sqlite3"
    app.config.update(
        DATABASE=database_path or os.environ.get("RSVP_DB_PATH", str(default_path)),
        MAX_CONTENT_LENGTH=8192,
    )
    initialize_database(app.config["DATABASE"])
    app.add_url_rule("/", view_func=index, methods=["GET"])
    app.add_url_rule("/api/rsvp", view_func=submit_rsvp, methods=["POST"])
    app.add_url_rule("/api/health", view_func=health, methods=["GET"])
    app.register_error_handler(HTTPException, handle_http_error)
    return app
