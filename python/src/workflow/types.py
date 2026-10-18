from __future__ import annotations

from typing import Any, Literal, TypedDict
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Snapshot(Model):
    campaign_id: str
    product: str
    data_version: str
    audience_version: str = "a1"
    impressions: int = Field(default=10000, ge=0)
    clicks: int = Field(default=400, ge=0)
    conversions: int = Field(default=40, ge=0)
    cost: float = Field(default=400, ge=0)
    revenue: float = Field(default=1200, ge=0)
    budget: float = Field(default=1000, gt=0)
    bid: float = Field(default=1, gt=0)
    audience_changed: bool = False
    audience_due: bool = False
    review_due: bool = False
    creative_requested: bool = False
    bidding_requested: bool = False
    budget_requested: bool = False
    urgent: bool = False

    @model_validator(mode="after")
    def consistent(self):
        if not self.conversions <= self.clicks <= self.impressions:
            raise ValueError("require conversions <= clicks <= impressions")
        return self

    def metrics(self) -> dict:
        return {"ctr": self.clicks / self.impressions if self.impressions else None,
                "cvr": self.conversions / self.clicks if self.clicks else None,
                "cpa": self.cost / self.conversions if self.conversions else None,
                "roas": self.revenue / self.cost if self.cost else None}


class MemoryRecord(Model):
    record_id: str
    campaign_id: str
    task_type: str = "creative"
    timestamp: int
    kind: Literal["constraint", "observation", "experience", "decision"] = "experience"
    content: str
    status: str = "insufficient_evidence"
    expires_at: int | None = None
    supersedes: str | None = None
    banned_terms: list[str] = Field(default_factory=list)
    required_terms: list[str] = Field(default_factory=list)
    evidence_version: str = ""
    action_id: str = ""


class WorkflowInput(Model):
    event_id: str
    simulated_time: int = Field(ge=0)
    snapshots: list[Snapshot]
    task: str = "optimize_campaigns"
    config_version: str = "v1"
    current_banned: dict[str, list[str]] = Field(default_factory=dict)
    current_required: dict[str, list[str]] = Field(default_factory=dict)
    memory_updates: list[MemoryRecord] = Field(default_factory=list)
    faults: dict[str, list[str]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_campaigns(self):
        ids = [s.campaign_id for s in self.snapshots]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate campaign IDs")
        if any(r.timestamp > self.simulated_time for r in self.memory_updates):
            raise ValueError("future memory cannot be ingested before its event time")
        return self


class Alert(Model):
    campaign_id: str
    type: str
    value: float | None = None


class RouteDecision(Model):
    campaign_id: str
    branches: list[str]
    reason: str
    signature: str
    reused: bool = False
    waiting: bool = False


class Variant(Model):
    headline: str = Field(min_length=1)
    description: str = Field(min_length=1)
    cta_text: str = Field(min_length=1)
    target_emotion: str = "benefit"


class CreativeOutput(Model):
    campaign_id: str
    variants: list[Variant] = Field(min_length=1, max_length=5)


class Action(Model):
    campaign_id: str
    action_type: Literal["create_creative", "adjust_bid", "adjust_budget", "pause_campaign", "pause_creative", "start_ab_test"]
    target_id: str
    value: Any = None
    evidence_version: str
    reason: str


class RunResult(Model):
    event_id: str
    status: str
    routes: list[RouteDecision] = Field(default_factory=list)
    executed_branches: dict[str, list[str]] = Field(default_factory=dict)
    reused_branches: dict[str, list[str]] = Field(default_factory=dict)
    proposed_actions: list[Action] = Field(default_factory=list)
    action_results: list[dict] = Field(default_factory=list)
    raw_creatives: list[dict] = Field(default_factory=list)
    error: str | None = None
    elapsed_ms: float = 0
    iteration: int = 0


class WorkflowState(TypedDict, total=False):
    event: WorkflowInput
    alerts: list[Alert]
    routes: list[RouteDecision]
    audience: dict[str, dict]
    creatives: dict[str, CreativeOutput]
    bids: dict[str, float]
    result: RunResult


class ExperimentConfig(Model):
    groups: list[Literal["B0", "B1", "B2", "B3", "M-Full"]] = Field(default_factory=lambda: ["B0", "B1", "B2", "B3", "M-Full"])
    mode: Literal["offline", "live"] = "offline"
    split: Literal["dev", "test"] = "dev"
    cases_per_category: int | None = Field(default=None, ge=1, le=30)
    repetitions: int = Field(default=1, ge=1)
    seed: int = 42
    cache_modes: list[Literal["cold", "warm"]] = Field(default_factory=lambda: ["cold", "warm"])
    output_dir: str = "artifacts/benchmark"
    max_iterations: int = Field(default=1, ge=1, le=3)
    observation_seconds: int = Field(default=60, ge=1)
    cache_ttl_seconds: int = Field(default=120, ge=1)
    memory_top_k: int = Field(default=5, ge=0)
    memory_token_budget: int = Field(default=2000, ge=0)
    context_token_limit: int = Field(default=16000, ge=256)
    max_output_tokens: int = Field(default=600, ge=64)
    max_requests: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=1)
    timeout_seconds: float = Field(default=30, gt=0)
    temperature: float | None = 0
    thinking_mode: Literal["enabled", "disabled"] | None = None
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)
    history_mode: Literal["end_to_end", "fixed"] = "end_to_end"

    @model_validator(mode="after")
    def valid_config(self):
        if self.split == "dev" and self.cases_per_category is not None and self.cases_per_category > 10:
            raise ValueError("frozen development split has 10 cases per category; use test for up to 30")
        if not self.groups or not self.cache_modes:
            raise ValueError("groups and cache_modes cannot be empty")
        if len(set(self.groups)) != len(self.groups) or len(set(self.cache_modes)) != len(self.cache_modes):
            raise ValueError("duplicate experiment groups or cache modes")
        if self.mode == "live" and (self.max_requests is None or self.max_tokens is None):
            raise ValueError("live experiments require max_requests and max_tokens")
        if self.max_output_tokens >= self.context_token_limit:
            raise ValueError("output token reservation must be smaller than context limit")
        return self
