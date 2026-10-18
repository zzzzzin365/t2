from __future__ import annotations

import random
from pydantic import Field

from ..workflow.telemetry import fingerprint
from ..workflow.types import MemoryRecord, Model, Snapshot, WorkflowInput

SCENARIO_VERSION = "synthetic-v1"
CATEGORIES = ["stable", "creative", "bidding", "audience", "mixed", "memory", "recovery"]


class Expectation(Model):
    required_branches: dict[str, list[str]] = Field(default_factory=dict)
    required_actions: dict[str, list[str]] = Field(default_factory=dict)
    no_actions: bool = False
    banned: dict[str, list[str]] = Field(default_factory=dict)
    required_terms: dict[str, list[str]] = Field(default_factory=dict)
    allowed_status: list[str] = Field(default_factory=lambda: ["completed", "waiting_for_observation"])


class Scenario(Model):
    case_id: str
    category: str
    split: str
    seed: int
    events: list[WorkflowInput]
    expectations: list[Expectation]


def make_scenarios(split: str = "dev", per_category: int | None = None, seed: int = 42) -> list[Scenario]:
    count = per_category or (10 if split == "dev" else 30)
    result = []
    for category in CATEGORIES:
        for index in range(count):
            case_seed = int(fingerprint([SCENARIO_VERSION, split, category, index, seed])[:12], 16)
            rng = random.Random(case_seed)
            cid = f"{split}_{category}_{index:03d}"
            n = rng.randint(2, 4)
            ids = [f"{cid}_camp_{i}" for i in range(n)]
            products = [rng.choice(["便携水杯", "无线耳机", "阅读灯", "双肩背包"]) for _ in ids]
            affected = ids if category in {"audience", "mixed"} and index % 2 else ids[:1]
            events, expected = [], []
            # A response-independent event schedule. t=70 specifically tests post-observation reuse.
            for step, now in enumerate([0, 10, 70, 90, 100, 180]):
                version = 1 if step < 3 else 2 if step < 5 else 3
                fresh = step in {0, 3, 5}
                snapshots, branch_map, action_map = [], {}, {}
                for i, campaign in enumerate(ids):
                    snap = Snapshot(campaign_id=campaign, product=products[i], data_version=f"v{version}",
                                    audience_version=f"a{version if category in {'audience', 'mixed'} else 1}",
                                    impressions=10000 + (index % 7) * 100, clicks=400, conversions=40,
                                    cost=400 + index, revenue=1200 + index)
                    branches, actions = [], []
                    if campaign in affected:
                        if category in {"creative", "memory", "recovery", "mixed"}:
                            snap.creative_requested = True
                            branches.append("creative")
                            actions += ["create_creative", "start_ab_test"]
                        if category in {"bidding", "mixed"}:
                            snap.bidding_requested = True
                            snap.budget_requested = index % 2 == 0
                            branches.append("bidding")
                            actions.append("adjust_bid")
                            if snap.budget_requested:
                                actions.append("adjust_budget")
                        if category == "audience":
                            snap.audience_changed = version > 1
                            snap.audience_due = version == 1
                            branches = ["audience", "creative", "bidding"]
                            actions = ["create_creative", "start_ab_test", "adjust_bid"]
                        if category == "mixed":
                            snap.audience_due = True
                            branches.insert(0, "audience")
                    snapshots.append(snap)
                    if fresh and branches:
                        branch_map[campaign] = branches
                        action_map[campaign] = actions
                updates = []
                bans, requires = {}, {}
                if category == "memory":
                    # Evaluation knows effective historical constraints; model sees them only through memory mode.
                    for campaign in affected:
                        banned = "限时" if version == 1 else "精选"
                        required = "专业" if version == 1 else "实用"
                        bans[campaign], requires[campaign] = [banned], [required]
                        if step in {0, 3}:
                            updates.append(MemoryRecord(record_id=f"{campaign}:policy:{version}", campaign_id=campaign,
                                                        timestamp=now, kind="constraint", status="confirmed",
                                                        supersedes=f"{campaign}:policy:1" if version == 2 else None,
                                                        banned_terms=[banned], required_terms=[required],
                                                        content=f"历史审核要求：禁止出现{banned}，必须提到{required}。"))
                        if step == 0:
                            for j in range(24):
                                updates.append(MemoryRecord(record_id=f"{campaign}:experience:{j}", campaign_id=campaign,
                                                            timestamp=0, task_type="creative" if j % 3 else "audience",
                                                            kind="experience", status="insufficient_evidence",
                                                            content=f"历史任务 {j}：记录产品展示、适用人群与文案讨论。" + "该条仅为模拟过程记录，不证明真实效果。" * 4))
                            updates.append(MemoryRecord(record_id=f"{campaign}:expired", campaign_id=campaign, timestamp=0,
                                                        kind="constraint", expires_at=0, banned_terms=[products[0]], content="已过期约束，不应使用"))
                faults = {}
                if category == "recovery" and fresh:
                    fault = ["after_commit", "timeout", "incomplete"][index % 3]
                    faults = {"apply_action": [fault]}
                    if step == 0:
                        faults["metrics"] = ["timeout"]
                events.append(WorkflowInput(event_id=f"{cid}:event:{step}", simulated_time=now,
                                            snapshots=snapshots, memory_updates=updates, faults=faults))
                expected.append(Expectation(required_branches=branch_map, required_actions=action_map,
                                            no_actions=not bool(action_map), banned=bans, required_terms=requires))
            result.append(Scenario(case_id=cid, category=category, split=split, seed=case_seed, events=events, expectations=expected))
    return result
