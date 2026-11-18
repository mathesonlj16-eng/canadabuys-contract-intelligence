"""Read-only comparison of sanitized procurement alerts against the local notice corpus.

This is an audit, not a collector: alert rows never become official notices,
and absence from a local snapshot is not evidence that a solicitation is open.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from . import core

DEFAULT_ALERTS = core.ROOT / "fixtures" / "email_alerts_2026-11-12.json"


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (value or "").casefold())


def load_alerts(path: Path | str = DEFAULT_ALERTS) -> list[dict]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    records = payload["alerts"] if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("Expected an array of email-alert records")
    seen = set()
    result = []
    for record in records:
        if not isinstance(record, dict) or not all(
            isinstance(record.get(k), str) and record[k].strip()
            for k in ("platform", "reference", "title", "buyer", "scope")
        ):
            raise ValueError("Each alert needs platform, reference, title, buyer, scope")
        if record["scope"] not in ("federal", "non_federal"):
            raise ValueError("Scope must be federal or non_federal")
        identity = (record["platform"], _key(record["reference"]))
        if identity in seen:
            raise ValueError("Duplicate alert identity")
        seen.add(identity)
        result.append(record)
    return result


def audit_alerts(con: sqlite3.Connection, alerts: list[dict]) -> dict:
    """No database writes, network calls, or changes to ranking decisions."""
    raw = [dict(r) for r in con.execute(
        "SELECT id, source_id, source_key, solicitation, title, buyer, "
        "first_seen, last_seen FROM notices"
    )]
    # Use the same ranking path as the dashboard; do not reimplement eligibility.
    recommended = core.get_notices(con, view="recommended", limit=1000)
    recommendable = {n["id"] for n in recommended["notices"]}
    ranking_complete = recommended["total"] == recommended["returned"]
    outcomes = []
    for alert in alerts:
        ref = _key(alert["reference"])
        exact = [r for r in raw if ref and (
            _key(r["source_key"]) == ref or _key(r["solicitation"]) == ref
        )]
        # A non-federal alert cannot be confirmed by an unrelated federal ID.
        federal_exact = next((
            r for r in exact
            if r["source_id"] == "canadabuys" and alert["scope"] == "federal"
        ), None)
        other_exact = next((r for r in exact if r["source_id"] != "canadabuys"), None)
        cross_scope_exact = next((
            r for r in exact if r["source_id"] == "canadabuys"
        ), None) if alert["scope"] != "federal" else None
        hint = _key(alert.get("match_hint", ""))
        title_key = _key(alert["title"])
        # Suggestive matches are intentionally never counted as collection proof.
        possible = cross_scope_exact
        if not federal_exact and not other_exact and not possible:
            for row in raw:
                row_title = _key(row["title"])
                if ((len(hint) >= 6 and hint in row_title)
                        or (len(title_key) >= 25 and
                            (title_key in row_title or row_title in title_key)
                            and len(row_title) >= 25)):
                    possible = row
                    break
        selected = federal_exact or other_exact or possible
        detail = core.get_detail(con, selected["id"]) if selected else None
        # Compare the published raw calendar date only. Converting time zones
        # would risk reporting a false conflict for local-time vendor alerts.
        date_match = re.match(r"^\d{4}-\d{2}-\d{2}", detail["close_raw"] or "") if detail else None
        stored_date = date_match.group(0) if date_match else None
        deadline_conflict = (
            bool(stored_date and alert.get("deadline_reported"))
            and stored_date != alert["deadline_reported"]
        )
        if federal_exact:
            status = "FOUND_IN_FEDERAL_FEED"
        elif other_exact:
            status = "FOUND_IN_OTHER_SOURCE"
        elif possible:
            status = "POSSIBLE_MATCH_REVIEW"
        elif alert["scope"] == "federal":
            status = "NOT_FOUND_IN_LOCAL_FEDERAL"
        else:
            status = "OUTSIDE_AUTOMATED_SCOPE"
        outcomes.append({
            "platform": alert["platform"],
            "reference": alert["reference"],
            "title": alert["title"],
            "buyer": alert["buyer"],
            "deadline_reported": alert.get("deadline_reported", ""),
            "notice_type_reported": alert.get("notice_type", ""),
            "scope": alert["scope"],
            "status": status,
            "matched_id": selected["id"] if selected else None,
            "matched_source": selected["source_id"] if selected else None,
            "first_seen": selected["first_seen"] if selected else None,
            "last_seen": selected["last_seen"] if selected else None,
            "stored_deadline_raw_date": stored_date,
            "deadline_date_conflict": deadline_conflict,
            "record_state": detail["record_state"] if detail else None,
            "fit_score": detail["score"] if detail else None,
            "ranking_reason": detail["recommendation"] if detail else None,
            "in_recommended": (
                selected["id"] in recommendable if selected and ranking_complete else None
            ),
            "review_required": (
                status != "FOUND_IN_FEDERAL_FEED" or deadline_conflict
                or bool(selected and ranking_complete and selected["id"] not in recommendable)
            ),
        })
    statuses = Counter(r["status"] for r in outcomes)
    platforms = Counter(r["platform"] for r in outcomes)
    federal = [r for r in outcomes if r["scope"] == "federal"]
    last_run = con.execute(
        "SELECT MAX(finished_at) FROM ingest_runs "
        "WHERE source_id IN ('canadabuys-open', 'canadabuys-new') AND status='success'"
    ).fetchone()[0]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "sanitized supplier-alert cohort; buyer status/deadlines unverified",
        "last_successful_federal_import": last_run,
        "local_total_notices": len(raw),
        "total_alerts": len(outcomes),
        "by_platform": dict(platforms),
        "by_status": dict(statuses),
        "federal_exact_matches": sum(r["status"] == "FOUND_IN_FEDERAL_FEED" for r in federal),
        "federal_sample_size": len(federal),
        "note": (
            "Federal sample matching is not overall feed recall. Non-federal notices "
            "are outside the existing automated CanadaBuys feed unless independently "
            "imported. Possible matches require human review. Ranking is unverified "
            "when recommended results exceed the 1000-row audit limit."
        ),
        "records": outcomes,
    }
