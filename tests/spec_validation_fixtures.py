"""Explicit native records for reader tests; writer admission is tested separately."""


def native_validation(identity="validation", edition=1, **changes):
    record = dict(id=identity, validation_id=identity, receipt_id=identity,
        edition=edition, validation_edition=edition, is_current=True,
        spec_id="spec", board_id="board", reviewer_id="reviewer", reviewer_name=None,
        recommendation="approve", outcome="success", subject_version=1, head_revision=1,
        digests={}, threshold_violations=[], pinpoints=[], created_at="2026-10-02T12:00:00Z",
        resolved_thresholds={"min_spec_confidence": 70, "min_spec_clarity": 80,
            "min_spec_assertiveness": 80, "min_spec_decidability": 80, "max_spec_ambiguity": 30})
    for metric in ("confidence", "clarity", "assertiveness", "decidability", "ambiguity"):
        record[metric] = 10 if metric == "ambiguity" else 90
        record[metric + "_justification"] = "Explicit accepted fixture evidence."
    return {**record, **changes}
