"""CI-07: bounded, offline, non-federal supplier-alert candidate intake.

Supplier emails are discovery hints, not authoritative bids. No Gmail access,
network, SQLite writes, automatic promotion, or client contact occurs here.
"""
from __future__ import annotations

from collections import Counter
from datetime import date as calendar_date, datetime
from email.utils import parseaddr
import hashlib
from html import unescape
import json
from pathlib import Path
import re

from . import email_audit

ALLOWED_SENDERS = {
    "TenderPipe": {"support@tenderpipe.com"},
    "Bids & Tenders": {"dailybids@bidsandtenders.ca"},
}
MAX_MESSAGES = 50
MAX_BODY = 100_000
MAX_ALERTS = 500
DATE_FORMATS = ("%B %d, %Y", "%b %d, %Y")
PRIVATE_TEXT = re.compile(r"(?i)(?:[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+|https?://|mailto:|\bwww\.)")
TP_LINK = re.compile(
    r"(?m)^\[([^\]\n]{8,200})\]\((https://tenderpipe\.com/tender/[^)\s]+)\)"
)
TP_REFERENCE = re.compile(r"(?m)^Reference:\s*(\S[^\r\n]*)$")
BT_RECORD = re.compile(
    r"(?m)^\[([^\]\n]{8,220})\]\(https://[^)\s]+\)\s*\n\s*\n"
    r"([^\r\n]{3,150})\s*\n\s*\n"
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
    r"\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}\s+[AP]M\s+[A-Z]{2,5})"
)


def _clean(value: object, limit: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    # Do not preserve markup, control characters, mailbox metadata or unbounded text.
    value = re.sub(r"<[^>]*>", "", value)
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    return re.sub(r"\s+", " ", value).strip()[:limit]


def _identity(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _iso_date(value: str) -> str:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def _extract_tenderpipe(body: str) -> list[dict]:
    found = []
    links = list(TP_LINK.finditer(body))
    refs = list(TP_REFERENCE.finditer(body))
    for idx, ref in enumerate(refs):
        stop = refs[idx + 1].start() if idx + 1 < len(refs) else len(body)
        section = body[ref.end():stop]
        prior_end = refs[idx - 1].end() if idx else 0
        title_link = next((
            link for link in reversed(links)
            if prior_end <= link.start() < ref.start()
            and ref.start() - link.end() < 2500
            and link.group(1).strip().casefold() not in ("view tender", "view details")
        ), None)
        org = re.search(r"(?m)^Organization:\s*(.+)$", section)
        close = re.search(r"(?m)^Closing Date:\s*(.+)$", section)
        if not (title_link and org and close):
            continue
        date = _iso_date(close.group(1))
        if date:
            found.append({
                "platform": "TenderPipe", "reference": _clean(ref.group(1), 120),
                "title": _clean(title_link.group(1)),
                "buyer": _clean(org.group(1)),
                "deadline_reported": date, "scope": "unknown",
                "notice_type": "UNVERIFIED",
            })
    return found


def _extract_bids(body: str) -> list[dict]:
    found = []
    for m in BT_RECORD.finditer(body):
        raw_title = _clean(m.group(1))
        # Only known structured title patterns are parsed; never invent an ID.
        parts = re.match(
            r"^(?:(?:RFQ|RFP|RFI)\s*#?\s*)?"
            r"([A-Za-z0-9][A-Za-z0-9#./-]{2,32})\s+-\s+(.+)$", raw_title, re.I
        )
        date = _iso_date(" ".join(m.group(3).split()[:3]))
        if not parts or not date:
            continue
        title = _clean(parts.group(2))
        found.append({
            "platform": "Bids & Tenders", "reference": _clean(parts.group(1), 120),
            "title": title, "buyer": _clean(m.group(2)),
            "deadline_reported": date, "scope": "unknown",
            "notice_type": "UNVERIFIED",
        })
    return found


def _from_messages(payload: dict) -> tuple[list[dict], list[dict]]:
    messages = payload.get("messages")
    if not isinstance(messages, list) or len(messages) > MAX_MESSAGES:
        raise ValueError("messages must be an array of at most 50 entries")
    observations, rejected = [], []
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            rejected.append({"index": index, "reason": "invalid_message"})
            continue
        platform, sender, body = message.get("platform"), message.get("sender"), message.get("body")
        sender = parseaddr(sender)[1].casefold() if isinstance(sender, str) else ""
        # Sender headers alone are NOT authentication; only allow known source shapes.
        if not isinstance(platform, str) or sender not in ALLOWED_SENDERS.get(platform, ()):
            rejected.append({"index": index, "reason": "provider_sender_mismatch"})
            continue
        if not isinstance(body, str) or not body or len(body) > MAX_BODY:
            rejected.append({"index": index, "reason": "invalid_body"})
            continue
        parsed = _extract_tenderpipe(body) if platform == "TenderPipe" else _extract_bids(body)
        if not parsed:
            rejected.append({"index": index, "reason": "no_supported_records"})
        observations.extend(parsed)
    return observations, rejected


def load_observations(path: str | Path) -> tuple[list[dict], list[dict], str]:
    """Accept either CI-06 reviewed metadata or synthetic plain-text digest envelopes."""
    source = json.loads(Path(path).read_text(encoding="utf8"))
    if not isinstance(source, dict):
        raise ValueError("Input must be an object")
    if "messages" in source:
        observations, rejected = _from_messages(source)
        label = "untrusted_message_body_parsed"
    else:
        observations = email_audit.load_alerts(path)
        rejected = []
        label = "reviewed_metadata_fixture"
    if len(observations) > MAX_ALERTS:
        raise ValueError("Too many records")
    return observations, rejected, label


def build_report(observations: list[dict], rejected: list[dict] | None = None,
                 input_label: str = "offline") -> dict:
    """Stable grouped candidates. No official verified/active/pursuable designation."""
    rejected = list(rejected or [])
    candidates = {}
    by_platform = Counter()
    nonfederal = 0
    unknown_scope = 0
    for index, row in enumerate(observations):
        platform, ref, buyer, title = (
            _clean(row.get(key, "")) for key in ("platform", "reference", "buyer", "title")
        )
        scope = row.get("scope")
        if scope == "federal":
            continue
        if scope not in ("unknown", "non_federal"):
            rejected.append({"index": index, "reason": "invalid_scope"})
            continue
        if scope == "non_federal":
            nonfederal += 1
        else:
            unknown_scope += 1
        if platform not in ALLOWED_SENDERS or not all((ref, buyer, title)):
            rejected.append({"index": index, "reason": "invalid_candidate"})
            continue
        reported_type = _clean(row.get("notice_type", "UNVERIFIED"), 80)
        if any(PRIVATE_TEXT.search(unescape(value)) for value in (ref, buyer, title, reported_type)):
            rejected.append({"index": index, "reason": "private_candidate_field"})
            continue
        date = _clean(row.get("deadline_reported", ""), 20)
        if date:
            try:
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
                    raise ValueError("Invalid ISO date")
                calendar_date.fromisoformat(date)
            except ValueError:
                rejected.append({"index": index, "reason": "invalid_deadline"})
                continue
        by_platform[platform] += 1
        key = hashlib.sha256(
            (_identity(buyer) + ":" + _identity(ref)).encode("utf8")
        ).hexdigest()[:20]
        candidate = candidates.setdefault(key, {
            "candidate_id": "email-candidate:" + key,
            "reference": ref, "buyer": buyer, "title": title,
            "platforms": [], "deadline_observations": [],
            "title_observations": [], "reported_notice_types": [],
            "observations": 0,
            "identity_conflict": False, "deadline_conflict": False,
            "scope": scope,
            "state": "UNVERIFIED_EMAIL_CANDIDATE",
            "qualification": "UNKNOWN",
            "verified_buyer_url": None,
        })
        candidate["observations"] += 1
        if scope == "non_federal":
            candidate["scope"] = "non_federal"
        if platform not in candidate["platforms"]:
            candidate["platforms"].append(platform)
        if date and date not in candidate["deadline_observations"]:
            candidate["deadline_observations"].append(date)
        if title not in candidate["title_observations"]:
            candidate["title_observations"].append(title)
        if reported_type and reported_type not in candidate["reported_notice_types"]:
            candidate["reported_notice_types"].append(reported_type)
        candidate["deadline_conflict"] = len(candidate["deadline_observations"]) > 1
        candidate["identity_conflict"] = len(candidate["title_observations"]) > 1
    for item in candidates.values():
        item["platforms"].sort()
        item["deadline_observations"].sort()
        item["title_observations"].sort()
        item["reported_notice_types"].sort()
    result = sorted(candidates.values(), key=lambda r: r["candidate_id"])
    return {
        "schema": "ci.nonfederal_candidates.v1",
        "input_label": input_label,
        "source_status": "VENDOR_ALERTS_UNVERIFIED",
        "not_an_official_notice_directory": True,
        "total_input_observations": len(observations),
        "nonfederal_observations": nonfederal,
        "unknown_scope_observations": unknown_scope,
        "unique_email_candidates": len(result),
        "unique_nonfederal_candidates": sum(c["scope"] == "non_federal" for c in result),
        "unclassified_candidates": sum(c["scope"] == "unknown" for c in result),
        "pending_buyer_verification": len(result),
        "buyer_verified_qualified": None,
        "false_relevant": None,
        "duplicate_observations": sum(x["observations"] - 1 for x in result),
        "conflicting_candidates": sum(
            x["deadline_conflict"] or x["identity_conflict"] for x in result
        ),
        "by_platform": dict(sorted(by_platform.items())),
        "rejected": rejected,
        "candidates": result,
        "limitations": (
            "Email From/sender fields cannot authenticate origin; validate transport separately. "
            "Provider messages are discovery hints, not buyer-verified bids. "
            "Raw digest parsers leave jurisdiction unknown, including for federal notices. "
            "No exact cross-platform identity is inferred when buyer/reference differ."
        ),
    }


def run_file(path: str | Path) -> dict:
    observations, rejected, label = load_observations(path)
    return build_report(observations, rejected, label)
