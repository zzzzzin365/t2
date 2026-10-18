from __future__ import annotations

from collections import Counter
from ..workflow.telemetry import canonical
from ..workflow.types import RunResult
from .scenarios import Expectation


def evaluate(result: RunResult, expected: Expectation) -> dict:
    errors = []
    required_count, completed_count = 0, 0
    for cid, branches in expected.required_branches.items():
        done = set(result.executed_branches.get(cid, [])) | set(result.reused_branches.get(cid, []))
        required_count += len(branches)
        completed_count += len(set(branches) & done)
        if not set(branches) <= done:
            errors.append(f"missing branches for {cid}: {sorted(set(branches) - done)}")
    successful = Counter((a["campaign_id"], a["action_type"]) for a in result.action_results if a.get("success"))
    for cid, actions in expected.required_actions.items():
        for action in actions:
            if not successful[cid, action]:
                errors.append(f"missing successful action {cid}/{action}")
    allowed = {(cid, action) for cid, actions in expected.required_actions.items() for action in actions}
    # Judge raw plans, including proposals stopped by the shared executor.
    forbidden_proposals = sum((a.campaign_id, a.action_type) not in allowed for a in result.proposed_actions)
    if forbidden_proposals:
        errors.append(f"{forbidden_proposals} forbidden/unnecessary proposed actions")
    if expected.no_actions and result.proposed_actions:
        errors.append("expected no new action")
    constraint_total, constraint_pass = 0, 0
    for output in result.raw_creatives:
        cid = output["campaign_id"]
        bans, required = expected.banned.get(cid, []), expected.required_terms.get(cid, [])
        if not bans and not required:
            continue
        text = canonical(output["variants"]).lower()
        checks = [term.lower() not in text for term in bans] + [term.lower() in text for term in required]
        constraint_total += len(checks)
        constraint_pass += sum(checks)
        if not all(checks):
            errors.append(f"raw creative violates historical constraints: {cid}")
    if result.status not in expected.allowed_status:
        errors.append(f"unexpected status: {result.status}")
    return {"success": not errors, "errors": errors, "required_branches": required_count,
            "completed_required_branches": completed_count, "constraint_checks": constraint_total,
            "constraint_pass": constraint_pass, "forbidden_proposals": forbidden_proposals}
