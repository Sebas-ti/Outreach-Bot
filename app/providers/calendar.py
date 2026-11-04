"""Stage 4 - Scheduling (Google Calendar + Meet) and follow-up email (Gmail)."""
import asyncio
import base64
import logging
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from typing import Protocol
from zoneinfo import ZoneInfo

from ..models import Booking

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.freebusy",
    "https://www.googleapis.com/auth/gmail.send",
]


class CalendarService(Protocol):
    async def free_slots(self, n: int = 3) -> list[datetime]: ...
    async def is_available(self, start: datetime) -> tuple[bool, str]: ...
    async def book(self, email: str, name: str, start: datetime, summary: str, description: str) -> Booking: ...
    async def send_email(self, to: str, subject: str, html: str) -> str: ...


def candidate_slots(now: datetime, tz: ZoneInfo, duration_min: int, start_h: int, end_h: int,
                    days: int = 7, step_min: int = 30, lead_h: int = 2) -> list[datetime]:
    """Business-hour slots (Mon-Fri) starting at least `lead_h` hours from now."""
    now = now.astimezone(tz)
    earliest = now + timedelta(hours=lead_h)
    out = []
    for d in range(days + 1):
        day = (now + timedelta(days=d)).date()
        if day.weekday() >= 5:
            continue
        t = datetime(day.year, day.month, day.day, start_h, 0, tzinfo=tz)
        last = datetime(day.year, day.month, day.day, end_h, 0, tzinfo=tz) - timedelta(minutes=duration_min)
        while t <= last:
            if t >= earliest:
                out.append(t)
            t += timedelta(minutes=step_min)
    return out


def pick_spread(slots: list[datetime], n: int) -> list[datetime]:
    """Prefer offering options on different days / times (morning vs afternoon)."""
    picked: list[datetime] = []
    for s in slots:
        if all(s.date() != p.date() for p in picked):
            picked.append(s)
        if len(picked) == n:
            return sorted(picked)
    for s in slots:
        if s not in picked:
            picked.append(s)
        if len(picked) == n:
            break
    return sorted(picked)


def within_business_hours(start: datetime, tz: ZoneInfo, duration_min: int, start_h: int, end_h: int) -> tuple[bool, str]:
    local = start.astimezone(tz)
    if local.weekday() >= 5:
        return False, "es fin de semana"
    end = local + timedelta(minutes=duration_min)
    if local.hour < start_h or (end.hour, end.minute) > (end_h, 0):
        return False, f"está fuera del horario laboral ({start_h}:00 a {end_h}:00)"
    return True, ""


def overlaps(slot: datetime, duration: timedelta, busy: list[tuple[datetime, datetime]]) -> bool:
    end = slot + duration
    return any(slot < b_end and end > b_start for b_start, b_end in busy)


class GoogleCalendarService:
    def __init__(self, credentials_file: str, token_file: str, calendar_id: str, tz: ZoneInfo,
                 duration_min: int, start_h: int, end_h: int):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        if not Path(token_file).exists():
            raise FileNotFoundError(
                f"No existe {token_file}. Ejecuta: python scripts/google_auth.py"
            )
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            Path(token_file).write_text(creds.to_json())
        self.cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.calendar_id = calendar_id
        self.tz = tz
        self.duration = timedelta(minutes=duration_min)
        self.start_h, self.end_h = start_h, end_h

    def _busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        res = self.cal.freebusy().query(body={
            "timeMin": start.isoformat(), "timeMax": end.isoformat(),
            "timeZone": str(self.tz), "items": [{"id": self.calendar_id}],
        }).execute()
        busy = res["calendars"][self.calendar_id].get("busy", [])
        return [(datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])) for b in busy]

    async def free_slots(self, n: int = 3) -> list[datetime]:
        slots = candidate_slots(datetime.now(self.tz), self.tz, int(self.duration.total_seconds() // 60),
                                self.start_h, self.end_h)
        if not slots:
            return []
        busy = await asyncio.to_thread(self._busy, slots[0], slots[-1] + self.duration)
        free = [s for s in slots if not overlaps(s, self.duration, busy)]
        return pick_spread(free, n)

    async def is_available(self, start: datetime) -> tuple[bool, str]:
        ok, why = within_business_hours(start, self.tz, int(self.duration.total_seconds() // 60), self.start_h, self.end_h)
        if not ok:
            return ok, why
        busy = await asyncio.to_thread(self._busy, start, start + self.duration)
        if overlaps(start, self.duration, busy):
            return False, "el especialista ya tiene otra reunión a esa hora"
        return True, ""

    def _insert(self, body: dict) -> dict:
        return self.cal.events().insert(
            calendarId=self.calendar_id, body=body, conferenceDataVersion=1, sendUpdates="all"
        ).execute()

    async def book(self, email: str, name: str, start: datetime, summary: str, description: str) -> Booking:
        end = start + self.duration
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start.isoformat(), "timeZone": str(self.tz)},
            "end": {"dateTime": end.isoformat(), "timeZone": str(self.tz)},
            "attendees": [{"email": email, "displayName": name}],
            "conferenceData": {"createRequest": {
                "requestId": f"bot-{int(start.timestamp())}-{abs(hash(email)) % 10_000}",
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }},
            "reminders": {"useDefault": True},
        }
        ev = await asyncio.to_thread(self._insert, body)
        return Booking(event_id=ev["id"], start=start.isoformat(),
                       meet_link=ev.get("hangoutLink", ""), html_link=ev.get("htmlLink", ""))

    def _send(self, to: str, subject: str, html: str) -> dict:
        msg = MIMEText(html, "html", "utf-8")
        msg["to"], msg["subject"] = to, subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        return self.gmail.users().messages().send(userId="me", body={"raw": raw}).execute()

    async def send_email(self, to: str, subject: str, html: str) -> str:
        res = await asyncio.to_thread(self._send, to, subject, html)
        return res.get("id", "")


class MockCalendarService:
    """Writes invitations/emails to data/outbox so they can be shown in the demo."""

    def __init__(self, tz: ZoneInfo, duration_min: int, start_h: int, end_h: int, outbox: str = "data/outbox"):
        self.tz = tz
        self.duration_min, self.start_h, self.end_h = duration_min, start_h, end_h
        self.outbox = Path(outbox)
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.booked: list[Booking] = []
        self.sent: list[dict] = []

    async def free_slots(self, n: int = 3) -> list[datetime]:
        slots = candidate_slots(datetime.now(self.tz), self.tz, self.duration_min, self.start_h, self.end_h)
        return pick_spread(slots, n)

    async def is_available(self, start: datetime) -> tuple[bool, str]:
        ok, why = within_business_hours(start, self.tz, self.duration_min, self.start_h, self.end_h)
        if not ok:
            return ok, why
        if any(datetime.fromisoformat(b.start) == start for b in self.booked):
            return False, "el especialista ya tiene otra reunión a esa hora"
        return True, ""

    async def book(self, email: str, name: str, start: datetime, summary: str, description: str) -> Booking:
        b = Booking(event_id=f"mock-{len(self.booked) + 1}", start=start.isoformat(),
                    meet_link="https://meet.google.com/abc-mock-xyz")
        self.booked.append(b)
        (self.outbox / f"invite_{b.event_id}.txt").write_text(
            f"PARA: {email} ({name})\nEVENTO: {summary}\nINICIO: {start.isoformat()}\n"
            f"MEET: {b.meet_link}\n\n{description}\n", encoding="utf-8")
        return b

    async def send_email(self, to: str, subject: str, html: str) -> str:
        mid = f"mock-mail-{len(self.sent) + 1}"
        self.sent.append({"to": to, "subject": subject, "html": html})
        (self.outbox / f"{mid}.html").write_text(f"<!-- to: {to} | {subject} -->\n{html}", encoding="utf-8")
        return mid
