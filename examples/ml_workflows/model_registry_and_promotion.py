# examples/ml_workflows/model_registry_and_promotion.py
"""
Advanced model registry, evaluation, and staged promotion workflow with Flyte.
- Registers candidate models with metadata and metrics
- Performs shadow and canary evaluation
- Supports promotion rules, rollback, and audit logging
- Parallel batch evaluations and aggregate reporting
"""
from __future__ import annotations

import dataclasses
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from flytekit import Resources, map_task, task, workflow, dynamic

LOGGER_NAME = "flyte.registry"
logger = logging.getLogger(LOGGER_NAME)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


@dataclasses.dataclass
class RegisteredModel:
    name: str
    version: str
    uri: str
    metrics: Dict[str, float]
    stage: str  # "None" | "Shadow" | "Canary" | "Production"
    notes: Optional[str] = None


@dataclasses.dataclass
class PromotionPolicy:
    monitor: str = "accuracy"
    mode: str = "max"
    min_improvement: float = 0.005
    require_shadow: bool = True
    require_canary: bool = True


# Simulated registry (replace with real service or DB)
REGISTRY: Dict[Tuple[str, str], RegisteredModel] = {}


@task(requests=Resources(cpu="1", mem="1Gi"))
def register_model(name: str, version: str, uri: str, metrics: Dict[str, float], notes: Optional[str] = None) -> RegisteredModel:
    rm = RegisteredModel(name=name, version=version, uri=uri, metrics=metrics, stage="None", notes=notes)
    REGISTRY[(name, version)] = rm
    logger.info("Registered model %s:%s uri=%s", name, version, uri)
    return rm


@map_task
@task(requests=Resources(cpu="1", mem="1Gi"))
def shadow_eval(model_uri: str, dataset_uri: str) -> Dict[str, float]:
    # Placeholder for batch scoring; return mock metrics
    # Replace with real scoring against dataset_uri
    return {"latency_ms": 5.0, "request_error_rate": 0.0}


@map_task
@task(requests=Resources(cpu="1", mem="1Gi"))
def canary_eval(model_uri: str, traffic_pct: int) -> Dict[str, float]:
    # Placeholder; in real usage attach to live traffic
    return {"canary_traffic_pct": float(traffic_pct), "slo_ok": 1.0}


@task
def aggregate_metrics(metrics: List[Dict[str, float]]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for m in metrics:
        for k, v in m.items():
            out[k] = out.get(k, 0.0) + float(v)
    # average
    if metrics:
        for k in list(out.keys()):
            out[k] /= float(len(metrics))
    return out


@task
def compare_and_decide(candidate: RegisteredModel, current: Optional[RegisteredModel], policy: PromotionPolicy) -> str:
    # returns next stage to set or "Reject"
    if current is None:
        return "Shadow" if policy.require_shadow else ("Canary" if policy.require_canary else "Production")
    mon = policy.monitor
    cand = candidate.metrics.get(mon, float("nan"))
    base = current.metrics.get(mon, float("nan"))
    if policy.mode == "max":
        improved = cand >= base + policy.min_improvement
    else:
        improved = cand <= base - policy.min_improvement
    if not improved:
        return "Reject"
    return "Shadow"


@task
def set_stage(model: RegisteredModel, stage: str) -> RegisteredModel:
    model.stage = stage
    REGISTRY[(model.name, model.version)] = model
    logger.info("Model %s:%s stage -> %s", model.name, model.version, stage)
    return model


@dynamic
def staged_promotion(candidate: RegisteredModel, current: Optional[RegisteredModel], policy: PromotionPolicy) -> RegisteredModel:
    decision = compare_and_decide(candidate=candidate, current=current, policy=policy)
    if decision == "Reject":
        return set_stage(model=candidate, stage="None")

    # Shadow
    cand = set_stage(model=candidate, stage="Shadow")
    shadow_reports = shadow_eval.map(model_uri=[cand.uri] * 3, dataset_uri=["s3://data/test"] * 3)
    _ = aggregate_metrics(metrics=shadow_reports)
    if policy.require_canary:
        cand = set_stage(model=cand, stage="Canary")
        canary_reports = canary_eval.map(model_uri=[cand.uri] * 3, traffic_pct=[1, 5, 10])
        _ = aggregate_metrics(metrics=canary_reports)
    # Promote
    cand = set_stage(model=cand, stage="Production")
    return cand


@workflow
def model_promotion_wf(
    name: str = "rf_classifier",
    version: str = "v1",
    uri: str = "s3://models/rf:v1",
    metrics: Dict[str, float] = {"accuracy": 0.92},
) -> RegisteredModel:
    candidate = register_model(name=name, version=version, uri=uri, metrics=metrics)
    current = None  # could be looked up from a real registry
    policy = PromotionPolicy()
    return staged_promotion(candidate=candidate, current=current, policy=policy)
