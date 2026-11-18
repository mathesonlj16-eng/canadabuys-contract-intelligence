"""CI-06 offline coverage audit regression tests: no network or Gmail access."""
import csv
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest

from ci import core, email_audit


def official_csv(reference, title):
    fields = {
        "referenceNumber-numeroReference": reference,
        "title-titre-eng": title,
        "publicationDate-datePublication": "2026-11-01",
        "amendmentNumber-numeroModification": "0",
        "amendmentDate-dateModification": "",
        "tenderClosingDate-appelOffresDateCloture": (
            datetime.now(timezone.utc) + timedelta(days=20)
        ).isoformat(),
        "tenderStatus-appelOffresStatut-eng": "Open",
        "attachment-piecesJointes-eng": "",
        "contractingEntityName-nomEntitContractante-eng": "Example Buyer",
        "solicitationNumber-numeroSollicitation": "SOL-111",
        "noticeURL-URLavis-eng": "",
        "tenderDescription-descriptionAppelOffres-eng": "Data governance and HIPLAB work.",
    }
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(fields))
    writer.writeheader()
    writer.writerow(fields)
    return stream.getvalue()


def alert(reference, *, scope="federal", title="Data governance work", hint=""):
    return dict(
        platform="Test provider",
        reference=reference,
        title=title,
        buyer="Example Buyer",
        deadline_reported="2026-12-31",
        scope=scope,
        match_hint=hint,
    )


class EmailAuditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.con = core.connect(Path(temp.name) / "audit.sqlite3")
        self.addCleanup(self.con.close)

    def import_notice(self, ref, title):
        core.import_csv_text(
            self.con, official_csv(ref, title), "canadabuys-open",
            verified=True, published_utc=datetime.now(timezone.utc).isoformat()
        )

    def test_shipped_alerts_have_ten_distinct_metadata_only_entries(self):
        alerts = email_audit.load_alerts()
        self.assertEqual(len(alerts), 10)
        self.assertEqual(len(set((r["platform"], r["reference"]) for r in alerts)), 10)
        self.assertEqual(sum(r["scope"] == "federal" for r in alerts), 2)
        self.assertFalse(any("message_id" in r or "email_body" in r for r in alerts))

    def test_exact_reference_and_solicitation_are_counted(self):
        self.import_notice("cb-797-56171832", "Data governance work")
        result = email_audit.audit_alerts(
            self.con, [alert("cb-797-56171832"), alert("SOL-111")]
        )
        self.assertEqual(result["federal_exact_matches"], 2)
        self.assertEqual(
            [r["status"] for r in result["records"]],
            ["FOUND_IN_FEDERAL_FEED", "FOUND_IN_FEDERAL_FEED"],
        )
        self.assertEqual(result["records"][0]["matched_id"],
                         "canadabuys:cb-797-56171832")

    def test_nonfederal_unmatched_is_out_of_scope_not_importer_defect(self):
        result = email_audit.audit_alerts(self.con, [alert("C3-05-26", scope="non_federal")])
        self.assertEqual(result["records"][0]["status"], "OUTSIDE_AUTOMATED_SCOPE")
        self.assertEqual(result["federal_sample_size"], 0)

    def test_unmatched_federal_is_snapshot_not_global_absence(self):
        result = email_audit.audit_alerts(self.con, [alert("cb-797-56171832")])
        self.assertEqual(result["records"][0]["status"], "NOT_FOUND_IN_LOCAL_FEDERAL")
        self.assertEqual(result["federal_exact_matches"], 0)

    def test_hinted_title_is_only_possible_not_proven_match(self):
        self.import_notice("cb-123-99999999", "Transformation of the HIPLAB Toolbox")
        result = email_audit.audit_alerts(
            self.con, [alert("1110028655", title="RFP Transformation HIPLAB", hint="HIPLAB")]
        )
        self.assertEqual(result["records"][0]["status"], "POSSIBLE_MATCH_REVIEW")
        self.assertEqual(result["federal_exact_matches"], 0)
        self.assertEqual(result["records"][0]["matched_id"], "canadabuys:cb-123-99999999")

    def test_nonfederal_identifier_collision_is_not_counted_as_federal(self):
        self.import_notice("203683", "Intelligent Electronic Device Management Software")
        result = email_audit.audit_alerts(
            self.con, [alert("203683", scope="non_federal")]
        )
        self.assertEqual(result["records"][0]["status"], "POSSIBLE_MATCH_REVIEW")
        self.assertEqual(result["federal_exact_matches"], 0)

    def test_reported_date_difference_requires_buyer_check(self):
        self.import_notice("cb-797-56171832", "Data governance work")
        candidate = alert("cb-797-56171832")
        candidate["deadline_reported"] = "1900-01-01"
        result = email_audit.audit_alerts(self.con, [candidate])
        self.assertEqual(result["records"][0]["status"], "FOUND_IN_FEDERAL_FEED")
        self.assertTrue(result["records"][0]["deadline_date_conflict"])
        self.assertTrue(result["records"][0]["review_required"])
        self.assertIsNotNone(result["records"][0]["ranking_reason"])

    def test_readonly_logic_does_not_change_notices_versions_or_reviews(self):
        self.import_notice("cb-797-56171832", "Data governance work")
        changes = self.con.total_changes
        before = [
            self.con.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
            for name in ("notices", "notice_versions", "notice_reviews")
        ]
        email_audit.audit_alerts(self.con, [alert("cb-797-56171832")])
        after = [
            self.con.execute("SELECT COUNT(*) FROM " + name).fetchone()[0]
            for name in ("notices", "notice_versions", "notice_reviews")
        ]
        self.assertEqual(before, after)
        self.assertEqual(changes, self.con.total_changes)

    def test_duplicate_and_invalid_scope_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / "alerts.json"
            record = alert("a")
            file.write_text(json.dumps([record, record]), encoding="utf8")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                email_audit.load_alerts(file)
            file.write_text(json.dumps([alert("b", scope="other")]), encoding="utf8")
            with self.assertRaisesRegex(ValueError, "Scope"):
                email_audit.load_alerts(file)


if __name__ == "__main__":
    unittest.main()
