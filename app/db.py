"""SQLite persistence: contacts (state machine) + event log (audit trail)."""
import csv
import io
import json
import sqlite3
import threading
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

from .models import Contact, Status
from .persona import normalize_gender, normalize_language, normalize_style
from .utils import normalize_url

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL DEFAULT '',
    company TEXT DEFAULT '',
    title TEXT DEFAULT '',
    email TEXT DEFAULT '',
    linkedin_url TEXT DEFAULT '',
    linkedin_confidence REAL,
    consent_phone TEXT DEFAULT '',
    phone TEXT DEFAULT '',
    phone_source TEXT DEFAULT '',
    demo_scenario TEXT DEFAULT '',
    language TEXT DEFAULT '',
    gender TEXT DEFAULT '',
    stand_notes TEXT DEFAULT '',
    script_style TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'PENDING',
    call_id TEXT DEFAULT '',
    call_summary TEXT DEFAULT '',
    transcript TEXT DEFAULT '',
    recording_url TEXT DEFAULT '',
    qualification TEXT DEFAULT '{}',
    meeting_start TEXT DEFAULT '',
    meeting_link TEXT DEFAULT '',
    ended_reason TEXT DEFAULT '',
    call_duration_s REAL DEFAULT 0,
    call_cost REAL DEFAULT 0,
    updated_at TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER,
    ts TEXT NOT NULL,
    stage TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contacts_call ON contacts(call_id);
"""

# Accepts Spanish or English CSV headers
CSV_ALIASES = {
    "first_name": ["nombre", "first_name", "nombres"],
    "last_name": ["apellidos", "apellido", "last_name"],
    "company": ["empresa", "company", "compania"],
    "title": ["cargo", "title", "puesto"],
    "email": ["email", "correo"],
    "linkedin_url": ["linkedin_url", "linkedin"],
    "consent_phone": ["telefono_consentido", "consent_phone", "telefono_prueba"],
    "demo_scenario": ["escenario_demo", "demo_scenario"],
    "language": ["idioma", "language", "lang", "lengua"],
    "gender": ["genero", "género", "gender", "sexo"],
    "script_style": ["estilo", "estilo_guion", "modo_guion", "style"],
    "stand_notes": ["notas_stand", "interes_stand", "interés_stand", "notas", "notes", "stand"],
}

_CONTACT_FIELDS = {f.name for f in fields(Contact)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Repository:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(contacts)")}
            for col, ddl in [("ended_reason", "TEXT DEFAULT ''"), ("call_duration_s", "REAL DEFAULT 0"),
                             ("call_cost", "REAL DEFAULT 0"), ("language", "TEXT DEFAULT ''"),
                             ("gender", "TEXT DEFAULT ''"), ("stand_notes", "TEXT DEFAULT ''"),
                             ("script_style", "TEXT DEFAULT ''")]:
                if col not in cols:  # migrate databases created by older versions
                    self._conn.execute(f"ALTER TABLE contacts ADD COLUMN {col} {ddl}")
            self._conn.commit()

    # ---------- contacts ----------
    def _row_to_contact(self, row: sqlite3.Row) -> Contact:
        data = dict(row)
        data["status"] = Status(data["status"])
        data["qualification"] = json.loads(data.get("qualification") or "{}")
        for k, v in list(data.items()):
            if v is None and k in ("call_duration_s", "call_cost"):
                data[k] = 0.0
            elif v is None and k != "linkedin_confidence":
                data[k] = ""
        return Contact(**{k: v for k, v in data.items() if k in _CONTACT_FIELDS})

    def add_contact(self, **values) -> Contact:
        values = {k: (v or "").strip() if isinstance(v, str) else v
                  for k, v in values.items() if k in _CONTACT_FIELDS and k != "id"}
        cols = ", ".join(values) + ", updated_at"
        marks = ", ".join("?" for _ in values) + ", ?"
        with self._lock:
            cur = self._conn.execute(
                f"INSERT INTO contacts ({cols}) VALUES ({marks})", [*values.values(), _now()]
            )
            self._conn.commit()
            new_id = cur.lastrowid
        return self.get(new_id)

    def import_csv(self, text: str) -> int:
        # Excel (es-CO) saves with ';' and sometimes appends ';;;;' to comma-separated lines
        lines = [ln.rstrip().rstrip(";") for ln in text.lstrip("﻿").splitlines() if ln.strip(" ;,")]
        first = lines[0] if lines else ""
        delimiter = ";" if first.count(";") > first.count(",") else ","
        reader = csv.DictReader(io.StringIO("\n".join(lines)), delimiter=delimiter)
        header_map = {}
        for field_name, aliases in CSV_ALIASES.items():
            for h in reader.fieldnames or []:
                if h.strip(" ;").lower() in aliases:
                    header_map[field_name] = h
                    break
        if "first_name" not in header_map:
            raise ValueError("El CSV debe tener al menos la columna 'nombre'")
        count = 0
        for row in reader:
            values = {f: (row.get(h) or "").strip(" ;") for f, h in header_map.items()}
            if not (values.get("first_name") or "").strip():
                continue
            if values.get("linkedin_url"):
                values["linkedin_url"] = normalize_url(values["linkedin_url"])
            if "language" in values:
                values["language"] = normalize_language(values["language"])
            if "gender" in values:
                values["gender"] = normalize_gender(values["gender"])
            if "script_style" in values:
                values["script_style"] = normalize_style(values["script_style"])
            self.add_contact(**values)
            count += 1
        return count

    def get(self, contact_id: int) -> Contact | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
        return self._row_to_contact(row) if row else None

    def get_by_call_id(self, call_id: str) -> Contact | None:
        if not call_id:
            return None
        with self._lock:
            row = self._conn.execute("SELECT * FROM contacts WHERE call_id=?", (call_id,)).fetchone()
        return self._row_to_contact(row) if row else None

    def list(self, status: Status | None = None) -> list[Contact]:
        q, args = "SELECT * FROM contacts", ()
        if status:
            q, args = q + " WHERE status=?", (status.value,)
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY id", args).fetchall()
        return [self._row_to_contact(r) for r in rows]

    def update(self, contact_id: int, **values) -> Contact:
        if "qualification" in values and isinstance(values["qualification"], dict):
            values["qualification"] = json.dumps(values["qualification"], ensure_ascii=False)
        if "status" in values:
            values["status"] = Status(values["status"]).value
        values["updated_at"] = _now()
        sets = ", ".join(f"{k}=?" for k in values)
        with self._lock:
            self._conn.execute(f"UPDATE contacts SET {sets} WHERE id=?", [*values.values(), contact_id])
            self._conn.commit()
        return self.get(contact_id)

    def reset_all(self) -> None:
        with self._lock:
            # URLs found by the search (confidence set) are cleared; URLs given in the CSV are kept
            self._conn.execute(
                "UPDATE contacts SET linkedin_url='' WHERE linkedin_confidence IS NOT NULL"
            )
            self._conn.execute(
                "UPDATE contacts SET status='PENDING', phone='', phone_source='', call_id='', "
                "call_summary='', transcript='', recording_url='', qualification='{}', "
                "meeting_start='', meeting_link='', linkedin_confidence=NULL, ended_reason='', "
                "call_duration_s=0, call_cost=0"
            )
            self._conn.execute("DELETE FROM events")
            self._conn.commit()

    def reset_one(self, contact_id: int) -> Contact | None:
        """Back to PENDING (keeps the contact's input data and any LinkedIn URL that came in the base)."""
        with self._lock:
            self._conn.execute(
                "UPDATE contacts SET linkedin_url='' WHERE id=? AND linkedin_confidence IS NOT NULL", (contact_id,))
            self._conn.execute(
                "UPDATE contacts SET status='PENDING', phone='', phone_source='', call_id='', call_summary='', "
                "transcript='', recording_url='', qualification='{}', meeting_start='', meeting_link='', "
                "linkedin_confidence=NULL, ended_reason='', call_duration_s=0, call_cost=0, updated_at=? WHERE id=?",
                (_now(), contact_id))
            self._conn.commit()
        return self.get(contact_id)

    def delete_all(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM contacts")
            self._conn.execute("DELETE FROM events")
            self._conn.commit()

    # ---------- events ----------
    def log(self, contact_id: int | None, stage: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO events (contact_id, ts, stage, message) VALUES (?, ?, ?, ?)",
                (contact_id, _now(), stage, message),
            )
            self._conn.commit()

    def events(self, limit: int = 200, contact_id: int | None = None) -> list[dict]:
        q, args = "SELECT * FROM events", []
        if contact_id is not None:
            q, args = q + " WHERE contact_id=?", [contact_id]
        with self._lock:
            rows = self._conn.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [dict(r) for r in rows]
