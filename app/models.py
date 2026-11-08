from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    PENDING = "PENDING"
    SEARCHING_LINKEDIN = "SEARCHING_LINKEDIN"
    ENRICHING = "ENRICHING"
    NO_DATA = "NO_DATA"  # no phone found -> skipped to next record
    BLOCKED = "BLOCKED"  # compliance guard (sandbox / no consent)
    CALLING = "CALLING"
    MEETING_BOOKED = "MEETING_BOOKED"
    FOLLOW_UP_SENT = "FOLLOW_UP_SENT"
    NOT_INTERESTED = "NOT_INTERESTED"
    REFERRED = "REFERRED"
    NOT_SERIOUS = "NOT_SERIOUS"  # prank / abuse / evidently false info -> agent hung up early
    NO_ANSWER = "NO_ANSWER"
    CALL_DONE = "CALL_DONE"
    ERROR = "ERROR"


TERMINAL = {
    Status.NO_DATA, Status.BLOCKED, Status.MEETING_BOOKED, Status.FOLLOW_UP_SENT,
    Status.NOT_INTERESTED, Status.REFERRED, Status.NOT_SERIOUS, Status.NO_ANSWER, Status.CALL_DONE, Status.ERROR,
}


@dataclass
class Contact:
    id: int
    first_name: str
    last_name: str
    company: str = ""
    title: str = ""
    email: str = ""
    linkedin_url: str = ""
    linkedin_confidence: float | None = None
    consent_phone: str = ""
    phone: str = ""
    phone_source: str = ""
    demo_scenario: str = ""
    language: str = ""  # es | en | pt (opening language)
    gender: str = ""  # f | m | '' (optional; inferred from the first name if empty)
    stand_notes: str = ""  # what they saw/asked at the booth (Word: "si se tiene el dato")
    status: Status = Status.PENDING
    call_id: str = ""
    call_summary: str = ""
    transcript: str = ""
    recording_url: str = ""
    qualification: dict[str, Any] = field(default_factory=dict)
    meeting_start: str = ""
    meeting_link: str = ""
    ended_reason: str = ""
    call_duration_s: float = 0.0
    call_cost: float = 0.0
    updated_at: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class LinkedInMatch:
    url: str
    confidence: float
    title: str = ""


@dataclass
class EnrichmentResult:
    found: bool
    phone: str = ""
    email: str = ""
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CallOutcome:
    call_id: str
    ended_reason: str = ""
    summary: str = ""
    transcript: str = ""
    recording_url: str = ""
    structured: dict[str, Any] = field(default_factory=dict)
    duration_s: float = 0.0
    cost: float = 0.0


@dataclass
class Booking:
    event_id: str
    start: str
    meet_link: str
    html_link: str = ""
