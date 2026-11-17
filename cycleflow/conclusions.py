from decimal import Decimal

from .contracts import DomainError, MissingDependency


# A job has at most one effective quality conclusion at any moment.
# Original test rows are never mutated or deleted; corrections and voids
# append chain records that move the effective pointer.

EFFECTIVE = "effective"
SUPERSEDED = "superseded"
VOIDED = "voided"


def chain_rows(db, job_id):
    """All test and correction nodes of a job, in application order."""
    rows = []
    for row in db.execute("SELECT * FROM tests WHERE job_id=?", (job_id,)):
        item = dict(row)
        item["node_type"] = "test"
        item["node_id"] = item["test_id"]
        item["recorded_at"] = item["tested_at"]
        item["action"] = "record"
        rows.append(item)
    for row in db.execute("SELECT * FROM test_corrections WHERE job_id=?", (job_id,)):
        item = dict(row)
        item["node_type"] = "correction"
        item["node_id"] = item["correction_id"]
        rows.append(item)
    rows.sort(key=lambda item: (item["recorded_at"], item["node_type"], item["node_id"]))
    return rows


def effective_conclusion(db, job_id):
    """Return the head of the current quality chain for a job.

    A void node stays ``effective`` as a tombstone so its reason and evidence
    remain part of the live history. A retest after a void starts a new chain
    segment, so all nodes before the last void belong to closed segments. The
    current segment must contain exactly one effective node; anything else is
    a storage fault that the integrity audit reports as ``dual_conclusion``.
    """
    rows = chain_rows(db, job_id)
    last_void = -1
    for index, row in enumerate(rows):
        if row["node_type"] == "correction" and row["action"] == "void" and row["state"] == EFFECTIVE:
            last_void = index
    current = rows[last_void + 1:] if last_void >= 0 else rows
    effective_nodes = [row for row in current if row["state"] == EFFECTIVE]
    if not effective_nodes:
        # The current segment is the void tombstone itself.
        return rows[last_void] if last_void >= 0 else None
    if len(effective_nodes) > 1:
        raise DomainError("dual_conclusion", f"job {job_id} has two effective conclusions", 500)
    return effective_nodes[0]


def resolve_target(db, target_type, target_id):
    if target_type == "test":
        row = db.execute("SELECT * FROM tests WHERE test_id=?", (target_id,)).fetchone()
    else:
        row = db.execute("SELECT * FROM test_corrections WHERE correction_id=?", (target_id,)).fetchone()
    if row is None:
        kind = "test" if target_type == "test" else "test correction"
        raise MissingDependency(kind, target_id)
    target = dict(row)
    target.setdefault("recorded_at", target.get("tested_at"))
    return target, target_type


def assert_target_effective(target, target_type, target_id):
    if target["state"] != EFFECTIVE:
        raise DomainError("correction_conflict",
                          f"target {target_type}:{target_id} is already {target['state']}", 409)
    if target.get("action") == "void":
        raise DomainError("correction_conflict",
                          f"target correction:{target_id} is a void tombstone; record a new test instead", 409)


def assert_no_cycle(db, target_type, target_id, *, new_correction_id):
    """Walk the target chain; the new node may never point at itself."""
    seen = set()
    current_type, current_id = target_type, target_id
    while True:
        key = (current_type, current_id)
        if key in seen:
            raise DomainError("cyclic_correction", "correction chain is already cyclic", 409)
        seen.add(key)
        if current_id == new_correction_id:
            raise DomainError("cyclic_correction", "correction would replace its own conclusion", 409)
        if current_type == "test":
            return
        row = db.execute(
            "SELECT target_type,target_id FROM test_corrections WHERE correction_id=?",
            (current_id,)).fetchone()
        if row is None:
            return
        current_type, current_id = row["target_type"], row["target_id"]


def _passes(db, node):
    threshold = db.execute("SELECT minimum_pass_rate FROM plans WHERE plan_id=?",
                           (node["plan_id"],)).fetchone()["minimum_pass_rate"]
    rate = Decimal(node["passed"]) / Decimal(node["passed"] + node["failed"])
    return rate >= Decimal(threshold)


def reconcile_job(db, job_id, *, event_id=None):
    """Recompute the job terminal state from its effective conclusion.

    Returns the effective node. A void node sitting at the chain head means no
    valid quality decision exists yet, so the job returns to the test gate.
    """
    node = effective_conclusion(db, job_id)
    if node is None:
        db.execute("UPDATE jobs SET status='awaiting_test',failure_reason=NULL WHERE job_id=?", (job_id,))
        return None
    if node["node_type"] == "correction" and node["action"] == "void":
        db.execute("UPDATE jobs SET status='awaiting_test',failure_reason='voided_conclusion' WHERE job_id=?",
                   (job_id,))
        retract_rework_qualifications(db, job_id, event_id)
        return node
    if _passes(db, node):
        db.execute("UPDATE jobs SET status='completed',failure_reason=NULL WHERE job_id=?", (job_id,))
        retract_rework_qualifications(db, job_id, event_id)
    else:
        db.execute("UPDATE jobs SET status='failed',failure_reason='quality_threshold' WHERE job_id=?",
                   (job_id,))
    return node


def retract_rework_qualifications(db, parent_job, event_id):
    """Reclassify rework eligibility when the parent no longer stands failed."""
    if event_id is None:
        return
    db.execute(
        "UPDATE rework_links SET state='retracted',retracted_event=? "
        "WHERE parent_job=? AND state='active'",
        (event_id, parent_job))


def record_rework_qualification(db, child_job, parent_job, event_id):
    """Persist the failed conclusion that qualifies a quality rework job."""
    node = effective_conclusion(db, parent_job)
    if node is None or (node["node_type"] == "correction" and node["action"] == "void"):
        raise DomainError("invalid_rework", "parent job has no effective failed conclusion")
    if _passes(db, node):
        raise DomainError("invalid_rework", "parent effective conclusion passes the quality gate")
    if node["node_type"] == "test":
        basis_test, basis_correction = node["node_id"], None
    else:
        basis_test, basis_correction = None, node["node_id"]
    db.execute(
        "INSERT INTO rework_links(child_job,parent_job,qualification_event,"
        "basis_test_id,basis_correction_id) VALUES(?,?,?,?,?)",
        (child_job, parent_job, event_id, basis_test, basis_correction))


def correction_summary(db, job_id):
    """Human-readable chain used by reports and trace views."""
    items = []
    for row in chain_rows(db, job_id):
        if row["node_type"] == "test":
            items.append({"node_type": "test", "test_id": row["test_id"], "state": row["state"],
                          "recorded_at": row["tested_at"], "passed": row["passed"],
                          "failed": row["failed"], "event_id": row["event_id"]})
        else:
            items.append({"node_type": "correction", "correction_id": row["correction_id"],
                          "action": row["action"], "state": row["state"],
                          "target_type": row["target_type"], "target_id": row["target_id"],
                          "recorded_at": row["recorded_at"], "passed": row["passed"],
                          "failed": row["failed"], "reason": row["reason"],
                          "evidence": row["evidence"], "event_id": row["event_id"]})
    return items
