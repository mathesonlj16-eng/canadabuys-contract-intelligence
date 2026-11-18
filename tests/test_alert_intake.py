"""Offline non-federal alert intake regressions. No Gmail, internet or SQLite."""
import json
from pathlib import Path
import tempfile
import unittest

from ci import alert_intake


TP = (
    "[Microsoft 365 Information Governance and Digital Workplace Modernization Initiative]"
    "(https://tenderpipe.com/tender/annapolis-example/)\n"
    "Reference: AC102626\n"
    "Organization: Municipality of the County of Annapolis\n"
    "Published: October 26, 2026\n"
    "Closing Date: November 26, 2026\n"
)
BT = (
    "[AC102626 - Microsoft 365 Information Governance and Digital Workplace Modernization Initiative]"
    "(https://tracking.example.invalid/link)\n\n"
    "Municipality of the County of Annapolis\n\n"
    "Nov 26, 2026 2:00 PM AST\n"
)
BT_HAMILTON = (
    "[C3-05-26 - Request for Information for Lease Administration and Real Estate Portfolio Management Software]"
    "(https://tracking.example.invalid/link)\n\n"
    "City of Hamilton\n\n"
    "Dec 16, 2026 3:00 PM EST\n"
)


def message(platform, sender, body):
    return {"platform": platform, "sender": sender, "body": body}


class AlertIntakeTests(unittest.TestCase):
    def test_real_sanitized_cohort_has_eight_unverified_nonfederal_candidates(self):
        result = alert_intake.run_file(
            alert_intake.email_audit.DEFAULT_ALERTS
        )
        self.assertEqual(result["total_input_observations"], 10)
        self.assertEqual(result["nonfederal_observations"], 8)
        self.assertEqual(result["unique_nonfederal_candidates"], 8)
        self.assertEqual(result["by_platform"], {"Bids & Tenders": 4, "TenderPipe": 4})
        self.assertEqual(result["unclassified_candidates"], 0)
        self.assertTrue(all(c["state"] == "UNVERIFIED_EMAIL_CANDIDATE"
                            and c["qualification"] == "UNKNOWN"
                            and c["verified_buyer_url"] is None
                            for c in result["candidates"]))

    def test_tenderpipe_plaintext_digest_parsing(self):
        parsed = alert_intake._extract_tenderpipe(TP)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["reference"], "AC102626")
        self.assertEqual(parsed[0]["deadline_reported"], "2026-11-26")
        self.assertEqual(parsed[0]["scope"], "unknown")

    def test_bids_and_tenders_plaintext_parsing(self):
        parsed = alert_intake._extract_bids(BT_HAMILTON)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["reference"], "C3-05-26")
        self.assertEqual(parsed[0]["buyer"], "City of Hamilton")
        self.assertEqual(parsed[0]["scope"], "unknown")

    def test_duplicate_mirror_and_repeated_email_dedup(self):
        with tempfile.TemporaryDirectory() as folder:
            file = Path(folder) / "digest.json"
            file.write_text(json.dumps({"messages": [
                message("TenderPipe", "support@tenderpipe.com", TP),
                message("TenderPipe", "support@tenderpipe.com", TP),
                message("Bids & Tenders", "dailybids@bidsandtenders.ca", BT),
            ]}), encoding="utf8")
            out1 = alert_intake.run_file(file)
            out2 = alert_intake.run_file(file)
            self.assertEqual(out1, out2)
            self.assertEqual(out1["unique_email_candidates"], 1)
            self.assertEqual(out1["unclassified_candidates"], 1)
            self.assertEqual(out1["duplicate_observations"], 2)
            self.assertEqual(out1["candidates"][0]["observations"], 3)
            self.assertEqual(out1["candidates"][0]["platforms"], [
                "Bids & Tenders", "TenderPipe"
            ])
            self.assertNotIn("tracking.example.invalid", json.dumps(out1))
            self.assertNotIn("tenderpipe.com/tender/", json.dumps(out1))

    def test_sender_mismatch_or_unknown_format_rejected(self):
        rows, rejected = alert_intake._from_messages({"messages": [
            message("TenderPipe", "support@tenderpipe.com.evil.example", TP),
            message("Bids & Tenders", "support@tenderpipe.com", BT),
            message(["TenderPipe"], "support@tenderpipe.com", TP),
            message("TenderPipe", "support@tenderpipe.com", "No reference here"),
        ]})
        self.assertEqual(rows, [])
        self.assertEqual([r["reason"] for r in rejected],
                         ["provider_sender_mismatch", "provider_sender_mismatch",
                          "provider_sender_mismatch",
                          "no_supported_records"])

    def test_conflicting_deadlines_are_quarantined_for_review(self):
        rows = [
            {"platform": "TenderPipe", "reference": "AC102626", "buyer": "Annapolis",
             "title": "Modernization", "deadline_reported": "2026-11-26", "scope": "non_federal"},
            {"platform": "Bids & Tenders", "reference": "AC102626", "buyer": "Annapolis",
             "title": "Modernization", "deadline_reported": "2026-12-03", "scope": "non_federal"},
        ]
        result = alert_intake.build_report(rows)
        self.assertEqual(result["unique_nonfederal_candidates"], 1)
        self.assertEqual(result["conflicting_candidates"], 1)
        self.assertTrue(result["candidates"][0]["deadline_conflict"])
        self.assertEqual(result["candidates"][0]["deadline_observations"],
                         ["2026-11-26", "2026-12-03"])
        self.assertEqual(result["candidates"][0]["qualification"], "UNKNOWN")

    def test_same_reference_different_buyer_never_mirrored(self):
        rows = [
            {"platform": "TenderPipe", "reference": "A-1", "buyer": "College A",
             "title": "Website", "scope": "non_federal"},
            {"platform": "Bids & Tenders", "reference": "A-1", "buyer": "City B",
             "title": "Website", "scope": "non_federal"},
        ]
        self.assertEqual(alert_intake.build_report(rows)["unique_nonfederal_candidates"], 2)

    def test_title_conflict_flagged_not_silently_overwritten(self):
        rows = [
            {"platform": "TenderPipe", "reference": "A-1", "buyer": "City A",
             "title": "WordPress website redesign", "scope": "non_federal"},
            {"platform": "Bids & Tenders", "reference": "A-1", "buyer": "City A",
             "title": "Industrial chemical supply", "scope": "non_federal"},
        ]
        result = alert_intake.build_report(rows)
        self.assertTrue(result["candidates"][0]["identity_conflict"])
        self.assertEqual(result["candidates"][0]["state"], "UNVERIFIED_EMAIL_CANDIDATE")

    def test_html_and_private_envelope_data_not_in_output(self):
        rows, rejected = alert_intake._from_messages({"messages": [
            dict(message("Bids & Tenders", "dailybids@bidsandtenders.ca", BT_HAMILTON),
                 gmail_message_id="private-message-123",
                 to="someone@example.com")
        ]})
        result = alert_intake.build_report(rows, rejected)
        self.assertNotIn("private-message-123", json.dumps(result))
        self.assertNotIn("someone@example.com", json.dumps(result))
        injected = dict(rows[0], title="<script>alert(1)</script>Software")
        cleaned = alert_intake.build_report([injected])
        self.assertNotIn("<script>", json.dumps(cleaned))
        for field, value in (
            ("title", "Contact someone&#64;example.com for details"),
            ("buyer", "City https://tracking.example.invalid/link"),
            ("reference", "mailto:someone@example.com"),
            ("notice_type", "RFI someone@example.com"),
        ):
            with self.subTest(field=field):
                result = alert_intake.build_report([dict(rows[0], **{field: value})])
                self.assertEqual(result["candidates"], [])
                self.assertEqual(result["rejected"], [{"index": 0, "reason": "private_candidate_field"}])
                self.assertNotIn("example.", json.dumps(result))

    def test_bad_or_empty_records_do_not_become_candidates(self):
        rows = [
            {"platform": "TenderPipe", "reference": "", "buyer": "City", "title": "Software",
             "scope": "non_federal"},
            {"platform": "TenderPipe", "reference": "A-2", "buyer": "City", "title": "Software",
             "scope": "non_federal", "deadline_reported": "yesterday"},
        ]
        result = alert_intake.build_report(rows)
        self.assertEqual(result["unique_nonfederal_candidates"], 0)
        self.assertEqual(len(result["rejected"]), 2)

    def test_bounded_input_count_and_oversized_body(self):
        with self.assertRaisesRegex(ValueError, "50"):
            alert_intake._from_messages({"messages": [{}] * 51})
        rows, rejected = alert_intake._from_messages({"messages": [
            message("TenderPipe", "support@tenderpipe.com", "x" * 100001)
        ]})
        self.assertEqual(rows, [])
        self.assertEqual(rejected[0]["reason"], "invalid_body")


if __name__ == "__main__":
    unittest.main()
