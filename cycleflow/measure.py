from decimal import Decimal

from . import conclusions
from .contracts import DomainError, MissingDependency, decimal_value, keys, nonnegative_int, required_text
from .execution import ExecutionRules


class MeasurementRules:
    def __init__(self, db):
        self.db = db
        self.jobs = ExecutionRules(db)

    def create_plan(self, payload, occurred_at):
        keys(payload, {"plan_id", "product", "revision", "minimum_pass_rate"})
        plan_id = required_text(payload["plan_id"], "plan_id")
        product = required_text(payload["product"], "product")
        revision = nonnegative_int(payload["revision"], "revision", positive=True)
        minimum = decimal_value(payload["minimum_pass_rate"], "minimum_pass_rate")
        if Decimal(minimum) > 1:
            raise DomainError("invalid_threshold", "minimum_pass_rate cannot exceed one")
        design = self.db.execute("SELECT design_id,released_at FROM designs WHERE product=? AND revision=?",
                                 (product, revision)).fetchone()
        if design is None:
            raise MissingDependency("design revision", f"{product}:{revision}")
        if occurred_at < design["released_at"]:
            raise DomainError("time_order", "plan precedes design release")
        self.db.execute("INSERT INTO plans(plan_id,product,revision,minimum_pass_rate,effective_at) VALUES(?,?,?,?,?)",
                        (plan_id, product, revision, minimum, occurred_at))
        return {"plan_id": plan_id, "product": product, "revision": revision, "threshold": minimum}

    def record(self, payload, occurred_at, event_id):
        keys(payload, {"test_id", "job_id", "plan_id", "passed", "failed"},
             {"test_id", "job_id", "plan_id", "passed", "failed", "note"})
        test_id = required_text(payload["test_id"], "test_id")
        job_id = required_text(payload["job_id"], "job_id")
        plan_id = required_text(payload["plan_id"], "plan_id")
        passed = nonnegative_int(payload["passed"], "passed")
        failed = nonnegative_int(payload["failed"], "failed")
        note = payload.get("note", "")
        if not isinstance(note, str) or len(note) > 500:
            raise DomainError("invalid_note", "note must be short text")
        job = self.jobs.get(job_id)
        if job["status"] != "awaiting_test":
            raise DomainError("job_not_testable", "job must complete integration before testing", 409)
        if passed + failed != job["quantity"]:
            raise DomainError("sample_mismatch", "tested units must equal job quantity")
        plan = self.get_plan(plan_id)
        if plan["product"] != job["design"]["product"] or plan["revision"] != job["design"]["revision"]:
            raise DomainError("plan_mismatch", "plan must match exact design revision")
        if occurred_at < max(plan["effective_at"], job["completed_at"]):
            raise DomainError("time_order", "test precedes plan or integration finish")
        rate = Decimal(passed) / Decimal(job["quantity"])
        accepted = rate >= Decimal(plan["minimum_pass_rate"])
        self.db.execute(
            "INSERT INTO tests(test_id,job_id,plan_id,tested_at,passed,failed,note,event_id,state) "
            "VALUES(?,?,?,?,?,?,?,?,'effective')",
            (test_id, job_id, plan_id, occurred_at, passed, failed, note, event_id))
        self.db.execute("UPDATE jobs SET status=?,failure_reason=? WHERE job_id=?",
                        ("completed" if accepted else "failed", None if accepted else "quality_threshold", job_id))
        return {"test_id": test_id, "job_id": job_id, "accepted": accepted,
                "pass_rate": str(rate), "threshold": plan["minimum_pass_rate"]}

    def correct(self, payload, occurred_at, event_id):
        keys(payload, {"correction_id", "target_type", "target_id", "passed", "failed",
                       "reason", "evidence"},
             {"correction_id", "target_type", "target_id", "passed", "failed",
              "reason", "evidence", "note"})
        correction_id = required_text(payload["correction_id"], "correction_id")
        target_type = required_text(payload["target_type"], "target_type")
        if target_type not in {"test", "correction"}:
            raise DomainError("invalid_field", "target_type must be test or correction")
        target_id = required_text(payload["target_id"], "target_id")
        passed = nonnegative_int(payload["passed"], "passed")
        failed = nonnegative_int(payload["failed"], "failed")
        reason = required_text(payload["reason"], "reason")
        evidence = required_text(payload["evidence"], "evidence")
        note = payload.get("note", "")
        if not isinstance(note, str) or len(note) > 500:
            raise DomainError("invalid_note", "note must be short text")
        if self.db.execute("SELECT 1 FROM test_corrections WHERE correction_id=?",
                           (correction_id,)).fetchone() is not None:
            raise DomainError("correction_exists", "correction_id already recorded", 409)
        target, target_type = conclusions.resolve_target(self.db, target_type, target_id)
        conclusions.assert_target_effective(target, target_type, target_id)
        job = self.jobs.get(target["job_id"])
        if job["status"] == "scrapped":
            raise DomainError("terminal_job", "cannot correct a scrapped job", 409)
        if passed + failed != job["quantity"]:
            raise DomainError("sample_mismatch", "corrected units must equal job quantity")
        plan = self.get_plan(target["plan_id"])
        # Corrections may arrive before or after the original event is ingested,
        # so only the plan's own effective time constrains business time.
        if occurred_at < plan["effective_at"]:
            raise DomainError("time_order", "correction precedes plan effectiveness")
        conclusions.assert_no_cycle(self.db, target_type, target_id, new_correction_id=correction_id)
        self.db.execute(
            "INSERT INTO test_corrections(correction_id,job_id,plan_id,action,target_type,target_id,"
            "recorded_at,passed,failed,reason,evidence,note,state,event_id) "
            "VALUES(?,?,?, 'correct', ?,?,?,?,?,?,?,?, 'effective', ?)",
            (correction_id, target["job_id"], target["plan_id"], target_type, target_id,
             occurred_at, passed, failed, reason, evidence, note, event_id))
        self._supersede(target_type, target_id)
        node = conclusions.reconcile_job(self.db, target["job_id"], event_id=event_id)
        rate = Decimal(passed) / Decimal(job["quantity"])
        return self._correction_result(correction_id, target["job_id"], "correct", rate,
                                       plan["minimum_pass_rate"], node)

    def void(self, payload, occurred_at, event_id):
        keys(payload, {"correction_id", "target_type", "target_id", "reason", "evidence"},
             {"correction_id", "target_type", "target_id", "reason", "evidence", "note"})
        correction_id = required_text(payload["correction_id"], "correction_id")
        target_type = required_text(payload["target_type"], "target_type")
        if target_type not in {"test", "correction"}:
            raise DomainError("invalid_field", "target_type must be test or correction")
        target_id = required_text(payload["target_id"], "target_id")
        reason = required_text(payload["reason"], "reason")
        evidence = required_text(payload["evidence"], "evidence")
        note = payload.get("note", "")
        if not isinstance(note, str) or len(note) > 500:
            raise DomainError("invalid_note", "note must be short text")
        if self.db.execute("SELECT 1 FROM test_corrections WHERE correction_id=?",
                           (correction_id,)).fetchone() is not None:
            raise DomainError("correction_exists", "correction_id already recorded", 409)
        target, target_type = conclusions.resolve_target(self.db, target_type, target_id)
        conclusions.assert_target_effective(target, target_type, target_id)
        job = self.jobs.get(target["job_id"])
        if job["status"] == "scrapped":
            raise DomainError("terminal_job", "cannot void a scrapped job", 409)
        plan = self.get_plan(target["plan_id"])
        if occurred_at < plan["effective_at"]:
            raise DomainError("time_order", "void precedes plan effectiveness")
        conclusions.assert_no_cycle(self.db, target_type, target_id, new_correction_id=correction_id)
        self.db.execute(
            "INSERT INTO test_corrections(correction_id,job_id,plan_id,action,target_type,target_id,"
            "recorded_at,passed,failed,reason,evidence,note,state,event_id) "
            "VALUES(?,?,?, 'void', ?,?,?, NULL,NULL,?,?,?, 'effective', ?)",
            (correction_id, target["job_id"], target["plan_id"], target_type, target_id,
             occurred_at, reason, evidence, note, event_id))
        self._supersede(target_type, target_id)
        node = conclusions.reconcile_job(self.db, target["job_id"], event_id=event_id)
        return self._correction_result(correction_id, target["job_id"], "void", None,
                                       plan["minimum_pass_rate"], node)

    def _supersede(self, target_type, target_id):
        if target_type == "test":
            self.db.execute("UPDATE tests SET state=? WHERE test_id=?",
                            (conclusions.SUPERSEDED, target_id))
        else:
            self.db.execute("UPDATE test_corrections SET state=? WHERE correction_id=?",
                            (conclusions.SUPERSEDED, target_id))

    def _correction_result(self, correction_id, job_id, action, rate, threshold, node):
        if action == "void":
            accepted = None
        else:
            accepted = rate >= Decimal(threshold)
        return {"correction_id": correction_id, "job_id": job_id, "action": action,
                "accepted": accepted,
                "pass_rate": str(rate) if rate is not None else None,
                "threshold": threshold,
                "job_status": self.jobs.get(job_id)["status"],
                "effective_node": None if node is None else
                {"node_type": node["node_type"], "node_id": node["node_id"]}}

    def get_plan(self, plan_id):
        row = self.db.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise MissingDependency("plan", plan_id)
        return dict(row)

    def tests_for_job(self, job_id):
        self.jobs.get(job_id)
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM tests WHERE job_id=? ORDER BY tested_at,test_id", (job_id,))]

    def trace(self, test_id):
        row = self.db.execute("SELECT * FROM tests WHERE test_id=?", (test_id,)).fetchone()
        if row is None:
            raise MissingDependency("test", test_id)
        test = dict(row)
        test["plan"] = self.get_plan(test["plan_id"])
        test["job_lineage"] = self.jobs.lineage(test["job_id"])
        test["reservations"] = self.jobs.inventory.reservations(test["job_id"])
        test["correction_chain"] = conclusions.correction_summary(self.db, test["job_id"])
        node = conclusions.effective_conclusion(self.db, test["job_id"])
        test["effective_conclusion"] = None if node is None else {
            "node_type": node["node_type"], "node_id": node["node_id"],
            "state": node["state"],
            "passed": node["passed"], "failed": node["failed"],
            "recorded_at": node["recorded_at"], "event_id": node["event_id"]}
        test["rework_links"] = [dict(row) for row in self.db.execute(
            "SELECT * FROM rework_links WHERE parent_job=? ORDER BY child_job", (test["job_id"],))]
        return test

    def correction_trace(self, correction_id):
        row = self.db.execute("SELECT * FROM test_corrections WHERE correction_id=?",
                              (correction_id,)).fetchone()
        if row is None:
            raise MissingDependency("test correction", correction_id)
        correction = dict(row)
        correction["plan"] = self.get_plan(correction["plan_id"])
        correction["chain"] = conclusions.correction_summary(self.db, correction["job_id"])
        node = conclusions.effective_conclusion(self.db, correction["job_id"])
        correction["effective_conclusion"] = None if node is None else {
            "node_type": node["node_type"], "node_id": node["node_id"],
            "state": node["state"], "event_id": node["event_id"]}
        correction["job_status"] = self.jobs.get(correction["job_id"])["status"]
        return correction

    def dispatch(self, kind, payload, occurred_at, event_id):
        if kind == "plan.create":
            return self.create_plan(payload, occurred_at)
        if kind == "test.record":
            return self.record(payload, occurred_at, event_id)
        if kind == "test.correct":
            return self.correct(payload, occurred_at, event_id)
        if kind == "test.void":
            return self.void(payload, occurred_at, event_id)
        raise DomainError("unknown_kind", f"unsupported measurement event {kind}")
