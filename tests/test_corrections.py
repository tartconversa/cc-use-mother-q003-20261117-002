from cycleflow.contracts import DomainError
from cycleflow.demo import END, START, event
from tests.common import ServiceCase


WINDOW = ("2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")


class CorrectionCase(ServiceCase):
    def _job_at_gate(self, passed=4, failed=0, test_id="T1"):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_job()
        self.complete_stages()
        self.ingest(20, "test.record", 30, {"test_id": test_id, "job_id": "J1",
                                            "plan_id": "P1", "passed": passed, "failed": failed})

    def _correct(self, number=21, minute=40, **overrides):
        payload = {"correction_id": "C1", "target_type": "test", "target_id": "T1",
                   "passed": 1, "failed": 3,
                   "reason": "review found mislabeled failures", "evidence": "NCR-2026-007"}
        payload.update(overrides)
        return self.ingest(number, "test.correct", minute, payload)

    def _report(self):
        return self.service.report("robot-arm", *WINDOW)


class CorrectionFlowTests(CorrectionCase):
    def test_correction_keeps_original_event_and_reconverges_job(self):
        self._job_at_gate(passed=4, failed=0)
        self.assertEqual(self.service.get_job("J1")["status"], "completed")
        result = self._correct()
        self.assertEqual(result["state"], "accepted")
        self.assertEqual(self.service.get_job("J1")["status"], "failed")
        job = self.service.get_job("J1")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["failure_reason"], "quality_threshold")
        # Original test row survives, only its lifecycle state moves.
        trace = self.service.get_test("T1")
        self.assertEqual(trace["state"], "superseded")
        self.assertEqual(trace["passed"], 4)
        self.assertEqual(trace["effective_conclusion"]["node_id"], "C1")
        chain = [item["node_id"] if "node_id" in item else item.get("test_id") or item["correction_id"]
                 for item in trace["correction_chain"]]
        self.assertEqual(chain, ["T1", "C1"])

    def test_yield_and_version_analysis_use_effective_result_only(self):
        self._job_at_gate(passed=4, failed=0)
        frozen_input = self._report()
        self.assertEqual(frozen_input["summary"]["passed_units"], 4)
        self._correct()
        report = self._report()
        self.assertEqual(report["summary"]["passed_units"], 1)
        self.assertEqual(report["summary"]["failed_units"], 3)
        self.assertEqual(report["summary"]["pass_rate"], "0.25")
        revision = report["revisions"][0]
        self.assertEqual(revision["passed_units"], 1)
        self.assertEqual(revision["failed_units"], 3)
        detail = report["jobs"][0]
        # Superseded numbers remain visible for explanation but do not double count.
        self.assertEqual(detail["tests"][0]["state"], "superseded")
        self.assertEqual(detail["effective_conclusion"]["node_id"], "C1")
        self.assertEqual(detail["effective_conclusion"]["passed"], 1)

    def test_wrong_failure_corrected_back_to_pass_reopens_completion(self):
        self._job_at_gate(passed=2, failed=2)
        self.assertEqual(self.service.get_job("J1")["status"], "failed")
        result = self._correct(passed=4, failed=0)
        self.assertEqual(result["state"], "accepted")
        detail = self.service.get_correction("C1")
        self.assertTrue(detail["effective_conclusion"]["node_id"] == "C1")
        self.assertEqual(self.service.get_job("J1")["status"], "completed")
        self.assertIsNone(self.service.get_job("J1")["failure_reason"])

    def test_duplicate_submission_replays_without_second_conclusion(self):
        self._job_at_gate()
        first = self._correct()
        again = self._correct()
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["repeated"])
        with self.service.store.read() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM test_corrections").fetchone()[0], 1)
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM tests WHERE job_id='J1' AND state='effective'").fetchone()[0], 0)

    def test_conflicting_correction_against_superseded_target_rejected(self):
        self._job_at_gate()
        self._correct()
        conflict = self._correct(number=22, minute=41, correction_id="C2",
                                 reason="rival claim", evidence="X")
        self.assertEqual(conflict["state"], "rejected")
        self.assertEqual(conflict["error_code"], "correction_conflict")
        # The first correction remains the unique effective conclusion.
        report = self._report()
        self.assertEqual(report["jobs"][0]["effective_conclusion"]["node_id"], "C1")

    def test_correction_chain_can_replace_a_correction(self):
        self._job_at_gate()
        self._correct()
        deeper = self.ingest(23, "test.correct", 42, {
            "correction_id": "C3", "target_type": "correction", "target_id": "C1",
            "passed": 0, "failed": 4, "reason": "deeper review", "evidence": "NCR-2026-008"})
        self.assertEqual(deeper["state"], "accepted")
        self.assertEqual(self.service.get_job("J1")["status"], "failed")
        report = self._report()
        self.assertEqual(report["summary"]["passed_units"], 0)
        self.assertEqual(report["jobs"][0]["effective_conclusion"]["node_id"], "C3")

    def test_cyclic_replacement_is_rejected(self):
        self._job_at_gate()
        self._correct()
        # C1 already supersedes T1; a new node cannot route back through itself.
        cyclic = self.ingest(23, "test.correct", 42, {
            "correction_id": "C3", "target_type": "correction", "target_id": "C3",
            "passed": 4, "failed": 0, "reason": "self loop", "evidence": "X"})
        self.assertEqual(cyclic["error_code"], "missing_dependency")

    def test_correction_counts_must_cover_job_quantity(self):
        self._job_at_gate()
        bad = self._correct(passed=1, failed=1)
        self.assertEqual(bad["state"], "rejected")
        self.assertEqual(bad["error_code"], "sample_mismatch")
        self.assertEqual(self.service.get_job("J1")["status"], "completed")

    def test_evidence_and_reason_are_required(self):
        self._job_at_gate()
        bad = self._correct(evidence="  ")
        self.assertEqual(bad["error_code"], "invalid_field")
        self.assertEqual(self.service.get_job("J1")["status"], "completed")

    def test_unknown_event_kinds_still_rejected(self):
        with self.assertRaises(DomainError) as caught:
            self.service.ingest(event(1, "test.revoke", 1, {}))
        self.assertEqual(caught.exception.code, "unknown_kind")

    def test_correction_id_reuse_with_different_content_rejected(self):
        self._job_at_gate()
        self._correct()
        reused = self.ingest(28, "test.correct", 41, {
            "correction_id": "C1", "target_type": "test", "target_id": "T1",
            "passed": 2, "failed": 2, "reason": "different claim", "evidence": "E2"})
        self.assertEqual(reused["error_code"], "correction_exists")
        self.assertEqual(self._report()["jobs"][0]["effective_conclusion"]["node_id"], "C1")

    def test_scrapped_job_cannot_be_corrected_or_voided(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_job()
        self.complete_stages()
        # Scrap at the test gate, before any quality verdict exists.
        self.ingest(27, "job.scrap", 29, {"job_id": "J1", "reason": "frame crack"})
        self.assertEqual(self.service.get_job("J1")["status"], "scrapped")
        correction = self.ingest(20, "test.correct", 40, {
            "correction_id": "C1", "target_type": "test", "target_id": "T1",
            "passed": 1, "failed": 3, "reason": "r", "evidence": "e"})
        self.assertEqual(correction["error_code"], "missing_dependency")

    def test_storage_fault_with_two_effective_nodes_is_detected(self):
        self._job_at_gate()
        with self.service.store.transaction() as db:
            db.execute(
                "INSERT INTO tests(test_id,job_id,plan_id,tested_at,passed,failed,note,state,event_id) "
                "VALUES('TX','J1','P1','2026-01-12T09:31:00+00:00',4,0,'','effective',12)")
        codes = {item["code"] for item in self.service.audit("robot-arm")["findings"]}
        self.assertIn("dual_conclusion", codes)


class OutOfOrderTests(CorrectionCase):
    def test_correction_before_original_test_waits_then_converges_on_retry(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_job()
        self.complete_stages()
        early = self._correct(minute=29)
        self.assertEqual(early["state"], "waiting")
        self.assertEqual(early["error_code"], "missing_dependency")
        # Original test arrives afterwards.
        self.ingest(20, "test.record", 30, {"test_id": "T1", "job_id": "J1",
                                            "plan_id": "P1", "passed": 4, "failed": 0})
        retried = self.service.retry(early["id"])
        self.assertEqual(retried["state"], "accepted")
        self.assertEqual(self.service.get_job("J1")["status"], "failed")
        report = self._report()
        self.assertEqual(report["summary"]["passed_units"], 1)

    def test_void_before_test_waits_and_does_not_block_later_record(self):
        self.seed_design()
        self.seed_lots()
        self.seed_plan()
        self.seed_job()
        self.complete_stages()
        early = self.ingest(30, "test.void", 29, {
            "correction_id": "V1", "target_type": "test", "target_id": "T1",
            "reason": "calibration", "evidence": "CAL-1"})
        self.assertEqual(early["state"], "waiting")
        self.ingest(20, "test.record", 30, {"test_id": "T1", "job_id": "J1",
                                            "plan_id": "P1", "passed": 4, "failed": 0})
        retried = self.service.retry(early["id"])
        self.assertEqual(retried["state"], "accepted")
        self.assertEqual(self.service.get_job("J1")["status"], "awaiting_test")
        # A fresh, valid test can now be recorded.
        second = self.ingest(31, "test.record", 35, {"test_id": "T2", "job_id": "J1",
                                                     "plan_id": "P1", "passed": 3, "failed": 1})
        self.assertEqual(second["state"], "accepted")
        self.assertEqual(self.service.get_job("J1")["status"], "completed")
        self.assertEqual(self._report()["jobs"][0]["effective_conclusion"]["node_id"], "T2")


class VoidTests(CorrectionCase):
    def test_void_returns_job_to_gate_and_excludes_units_from_yield(self):
        self._job_at_gate(passed=4, failed=0)
        voided = self.ingest(24, "test.void", 43, {
            "correction_id": "V1", "target_type": "test", "target_id": "T1",
            "reason": "equipment calibration invalidated run", "evidence": "CAL-99"})
        self.assertEqual(voided["state"], "accepted")
        job = self.service.get_job("J1")
        self.assertEqual(job["status"], "awaiting_test")
        self.assertEqual(job["failure_reason"], "voided_conclusion")
        report = self._report()
        self.assertEqual(report["summary"]["tested_units"], 0)
        self.assertIsNone(report["summary"]["pass_rate"])
        issue_codes = {item["code"] for item in report["quality"]["issues"]}
        self.assertIn("voided_conclusion", issue_codes)
        self.assertFalse(report["quality"]["publishable"])
        trace = self.service.get_test("T1")
        self.assertEqual(trace["state"], "superseded")
        self.assertEqual(trace["effective_conclusion"]["node_id"], "V1")

    def test_retest_after_void_becomes_unique_effective_conclusion(self):
        self._job_at_gate(passed=4, failed=0)
        self.ingest(24, "test.void", 43, {
            "correction_id": "V1", "target_type": "test", "target_id": "T1",
            "reason": "calibration", "evidence": "CAL-99"})
        retest = self.ingest(25, "test.record", 50, {"test_id": "T2", "job_id": "J1",
                                                     "plan_id": "P1", "passed": 3, "failed": 1})
        self.assertEqual(retest["state"], "accepted")
        report = self._report()
        self.assertEqual(report["summary"]["tested_units"], 4)
        self.assertEqual(report["summary"]["passed_units"], 3)
        self.assertEqual(report["jobs"][0]["effective_conclusion"]["node_id"], "T2")
        self.assertEqual(self.service.audit("robot-arm")["finding_count"], 0)

    def test_void_against_already_voided_chain_conflicts(self):
        self._job_at_gate()
        self.ingest(24, "test.void", 43, {
            "correction_id": "V1", "target_type": "test", "target_id": "T1",
            "reason": "calibration", "evidence": "CAL-99"})
        second_void = self.ingest(25, "test.void", 44, {
            "correction_id": "V2", "target_type": "correction", "target_id": "V1",
            "reason": "again", "evidence": "X"})
        self.assertEqual(second_void["error_code"], "correction_conflict")

    def test_correction_against_void_tombstone_is_rejected_use_retest(self):
        self._job_at_gate(passed=4, failed=0)
        self.ingest(24, "test.void", 43, {
            "correction_id": "V1", "target_type": "test", "target_id": "T1",
            "reason": "calibration", "evidence": "CAL-99"})
        revived = self.ingest(26, "test.correct", 45, {
            "correction_id": "C9", "target_type": "correction", "target_id": "V1",
            "passed": 4, "failed": 0, "reason": "calibration re-confirmed", "evidence": "CAL-100"})
        self.assertEqual(revived["error_code"], "correction_conflict")
        self.assertEqual(self.service.get_job("J1")["status"], "awaiting_test")


class ReworkConvergenceTests(CorrectionCase):
    def _failed_job_with_rework(self):
        self._job_at_gate(passed=2, failed=2)
        child = self.seed_job("J2", quantity=2, number=21, minute=31, parent="J1")
        self.assertEqual(child["state"], "accepted")
        return child

    def test_rework_qualification_records_failed_basis(self):
        self._failed_job_with_rework()
        job = self.service.get_job("J1")
        link = next(item for item in job["rework_links"] if item["child_job"] == "J2")
        self.assertEqual(link["state"], "active")
        self.assertEqual(link["basis_test_id"], "T1")
        self.assertEqual(self.service.audit("robot-arm")["finding_count"], 0)

    def test_correcting_parent_to_pass_retracts_rework_eligibility(self):
        self._failed_job_with_rework()
        # Quality review: the original "failure" was itself a recording error.
        self.ingest(22, "test.correct", 40, {
            "correction_id": "C1", "target_type": "test", "target_id": "T1",
            "passed": 4, "failed": 0, "reason": "mislabeled units", "evidence": "NCR-1"})
        self.assertEqual(self.service.get_job("J1")["status"], "completed")
        link = next(item for item in self.service.get_job("J1")["rework_links"]
                    if item["child_job"] == "J2")
        self.assertEqual(link["state"], "retracted")
        self.assertIsNotNone(link["retracted_event"])
        report = self._report()
        self.assertEqual(report["summary"]["rework_jobs"], 1)
        self.assertEqual(report["summary"]["retracted_rework_jobs"], 1)
        # The existing child job is preserved, but no new rework can be opened.
        refused = self.seed_job("J3", quantity=1, number=23, minute=41, parent="J1")
        self.assertEqual(refused["error_code"], "invalid_rework")
        self.assertEqual(self.service.audit("robot-arm")["finding_count"], 0)

    def test_rework_basis_tracks_correction_that_remains_failed(self):
        self._failed_job_with_rework()
        # Counts change but the gate still fails, so the existing rework stands.
        self.ingest(22, "test.correct", 40, {
            "correction_id": "C1", "target_type": "test", "target_id": "T1",
            "passed": 1, "failed": 3, "reason": "recount", "evidence": "NCR-2"})
        link = next(item for item in self.service.get_job("J1")["rework_links"]
                    if item["child_job"] == "J2")
        self.assertEqual(link["state"], "active")
        self.assertEqual(self.service.get_job("J1")["status"], "failed")
        self.assertEqual(self.service.audit("robot-arm")["finding_count"], 0)

    def test_rework_after_void_loses_its_basis(self):
        self._failed_job_with_rework()
        self.ingest(24, "test.void", 43, {
            "correction_id": "V1", "target_type": "test", "target_id": "T1",
            "reason": "calibration", "evidence": "CAL-99"})
        link = next(item for item in self.service.get_job("J1")["rework_links"]
                    if item["child_job"] == "J2")
        self.assertEqual(link["state"], "retracted")
        self.assertEqual(self.service.get_job("J1")["status"], "awaiting_test")
        self.assertEqual(self.service.audit("robot-arm")["finding_count"], 0)


class FrozenReportTests(CorrectionCase):
    def test_frozen_report_keeps_original_values_but_freshness_explains_gap(self):
        self._job_at_gate(passed=4, failed=0)
        preview = self._report()
        self.service.publish("jan", "robot-arm", *WINDOW, preview["fingerprint"])
        self._correct()
        frozen = self.service.get_release("jan")
        self.assertEqual(frozen["report"]["summary"]["passed_units"], 4)
        self.assertEqual(frozen["report"]["summary"]["pass_rate"], "1")
        freshness = self.service.shelf.freshness("jan")
        self.assertTrue(freshness["current_differs"])
        self.assertEqual(freshness["current_summary"]["passed_units"], 1)
        explanation = freshness["explanations"][0]
        self.assertEqual(explanation["job_id"], "J1")
        self.assertEqual(explanation["frozen_status"], "completed")
        self.assertEqual(explanation["current_status"], "failed")
        self.assertEqual(explanation["frozen_conclusion"]["node_id"], "T1")
        self.assertEqual(explanation["current_conclusion"]["node_id"], "C1")
        self.assertEqual(explanation["corrections"][0]["evidence"], "NCR-2026-007")
        # Frozen fingerprint itself never moves.
        self.assertEqual(frozen["fingerprint"], self.service.get_release("jan")["fingerprint"])

    def test_audit_stays_clean_through_correction_and_publish_cycle(self):
        self._job_at_gate(passed=4, failed=0)
        self._correct()
        self.assertTrue(self.service.audit("robot-arm")["ok"])
        # A failed verdict is a valid terminal state and remains publishable;
        # voiding it is what reopens the quality gate and blocks publication.
        self.assertTrue(self._report()["quality"]["publishable"])
        self.ingest(24, "test.void", 43, {
            "correction_id": "V1", "target_type": "correction", "target_id": "C1",
            "reason": "run invalidated", "evidence": "NCR-9"})
        blocked = self._report()
        self.assertFalse(blocked["quality"]["publishable"])
        self.ingest(25, "test.record", 50, {"test_id": "T2", "job_id": "J1",
                                            "plan_id": "P1", "passed": 4, "failed": 0})
        resolved = self._report()
        self.assertTrue(resolved["quality"]["publishable"])
        self.service.publish("feb", "robot-arm", *WINDOW, resolved["fingerprint"])
        self.assertTrue(self.service.audit("robot-arm")["ok"])
