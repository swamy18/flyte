# examples/ml_workflows/hyperparameter_tuning.py
"""
Production-ready hyperparameter tuning utilities and Flyte workflows.

Features:
- Grid search across discrete/continuous ranges (expanded from specs)
- Bayesian optimization hooks (external optimizer callback-compatible)
- Parallel training/evaluation via map_task
- Custom metric tracking and aggregation
- Early stopping based on patience and best metric plateau
- Robust logging, retries, and error handling

This example uses scikit-learn for a concrete model (e.g., RandomForestClassifier),
but the interfaces are generic so you can adapt to any framework.
"""
from __future__ import annotations

import dataclasses
import itertools
import json
import logging
import math
import os
import random
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from flytekit import Resources, dynamic, map_task, task, workflow
from flytekit.types.file import FlyteFile
from flytekit import CurrentContext

# Optional: If scikit-learn isn't available in your container, replace with your framework
try:
    from sklearn.datasets import make_classification
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import accuracy_score
    from sklearn.model_selection import train_test_split
except Exception as e:  # pragma: no cover - handled at runtime
    RandomForestClassifier = None
    make_classification = None
    accuracy_score = None
    train_test_split = None


# -----------------------------
# Data and configuration models
# -----------------------------
@dataclasses.dataclass
class SearchSpace:
    discrete: Mapping[str, Sequence[Any]] = dataclasses.field(default_factory=dict)
    continuous: Mapping[str, Tuple[float, float, Optional[float]]] = dataclasses.field(
        default_factory=dict
    )
    # continuous param spec is (low, high, optional_step). If step is None, we will sample.


@dataclasses.dataclass
class EarlyStoppingConfig:
    monitor: str = "accuracy"  # name of metric to monitor
    mode: str = "max"  # "max" or "min"
    patience: int = 5  # number of non-improving iterations before stop
    min_delta: float = 1e-4  # significant change threshold


@dataclasses.dataclass
class TuningConfig:
    max_iterations: int = 50
    random_seed: int = 1337
    parallelism: int = 8
    early_stopping: EarlyStoppingConfig = dataclasses.field(default_factory=EarlyStoppingConfig)
    # Bayesian optimization hooks
    bo_explore_start: int = 10  # number of random/grid points before BO
    bo_callback_module: Optional[str] = None  # dotted path to a module with suggest() function


@dataclasses.dataclass
class TrainingDataConfig:
    n_samples: int = 2000
    n_features: int = 20
    n_informative: int = 10
    n_redundant: int = 2
    n_classes: int = 2
    test_size: float = 0.2
    random_state: int = 42


@dataclasses.dataclass
class TrialResult:
    params: Dict[str, Any]
    metrics: Dict[str, float]
    artifact: Optional[str] = None  # e.g., model path
    status: str = "ok"  # ok | failed
    error: Optional[str] = None


# -----------------------------
# Utility helpers
# -----------------------------
LOGGER_NAME = "flyte.hpo"
logger = logging.getLogger(LOGGER_NAME)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)


def seeded_random(seed: int) -> random.Random:
    r = random.Random()
    r.seed(seed)
    return r


def expand_grid(space: SearchSpace) -> List[Dict[str, Any]]:
    # Expand discrete grid. Continuous ranges with a step are discretized.
    grid_items: Dict[str, Sequence[Any]] = {}
    for k, v in space.discrete.items():
        grid_items[k] = list(v)
    for k, (low, high, step) in space.continuous.items():
        if step is None or step <= 0:
            # sample later; for grid, we create a small evenly spaced set
            n = 5
            step_val = (high - low) / max(n - 1, 1)
            grid_items[k] = [low + i * step_val for i in range(n)]
        else:
            n = int(math.floor((high - low) / step)) + 1
            grid_items[k] = [low + i * step for i in range(n)]

    keys = list(grid_items.keys())
    values = [grid_items[k] for k in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def load_bo_suggester(module_path: Optional[str]):
    if not module_path:
        return None
    try:
        import importlib

        return importlib.import_module(module_path)
    except Exception as e:
        logger.warning("Failed to import BO module %s: %s", module_path, e)
        return None


# -----------------------------
# Flyte tasks
# -----------------------------
@task(requests=Resources(cpu="1", mem="1Gi"), retries=2)
def generate_synthetic_data(cfg: TrainingDataConfig) -> Tuple[List[List[float]], List[int], List[List[float]], List[int]]:
    if make_classification is None or train_test_split is None:
        raise RuntimeError("scikit-learn not available in this image")

    X, y = make_classification(
        n_samples=cfg.n_samples,
        n_features=cfg.n_features,
        n_informative=cfg.n_informative,
        n_redundant=cfg.n_redundant,
        n_classes=cfg.n_classes,
        random_state=cfg.random_state,
    )
    X_train, X_val, y_train, y_val = train_test_split(
        X, y, test_size=cfg.test_size, random_state=cfg.random_state
    )
    return X_train.tolist(), y_train.tolist(), X_val.tolist(), y_val.tolist()


@task(requests=Resources(cpu="2", mem="2Gi"), retries=1)
def train_and_eval(
    params: Dict[str, Any],
    X_train: List[List[float]],
    y_train: List[int],
    X_val: List[List[float]],
    y_val: List[int],
) -> TrialResult:
    ts = time.time()
    try:
        if RandomForestClassifier is None or accuracy_score is None:
            raise RuntimeError("scikit-learn not available in this image")
        # Example: map generic params to RF params
        rf = RandomForestClassifier(
            n_estimators=int(params.get("n_estimators", 100)),
            max_depth=None if params.get("max_depth") in (None, "None") else int(params["max_depth"]),
            min_samples_split=int(params.get("min_samples_split", 2)),
            min_samples_leaf=int(params.get("min_samples_leaf", 1)),
            max_features=params.get("max_features", "sqrt"),
            random_state=int(params.get("random_state", 0)),
            n_jobs=-1,
        )
        rf.fit(X_train, y_train)
        preds = rf.predict(X_val)
        acc = float(accuracy_score(y_val, preds))
        metrics = {"accuracy": acc, "train_sec": time.time() - ts}
        # In a real system, persist model and return URI
        artifact_path = None
        return TrialResult(params=params, metrics=metrics, artifact=artifact_path, status="ok")
    except Exception as e:
        ctx = CurrentContext()
        logger.exception("Training failed: %s", e)
        return TrialResult(params=params, metrics={}, artifact=None, status="failed", error=str(e))


@map_task
@task(requests=Resources(cpu="1", mem="1Gi"), retries=0)
def parallel_eval(
    params: Dict[str, Any],
    X_train: List[List[float]],
    y_train: List[int],
    X_val: List[List[float]],
    y_val: List[int],
) -> TrialResult:
    # simple wrapper to reuse train_and_eval for map_task if needed
    return train_and_eval(params=params, X_train=X_train, y_train=y_train, X_val=X_val, y_val=y_val)


@task(retries=0)
def select_best(results: List[TrialResult], monitor: str, mode: str) -> TrialResult:
    valid = [r for r in results if r.status == "ok" and monitor in r.metrics]
    if not valid:
        # Return a failed result if everything failed
        return TrialResult(params={}, metrics={}, artifact=None, status="failed", error="No valid trials")
    reverse = mode == "max"
    best = sorted(valid, key=lambda r: r.metrics[monitor], reverse=reverse)[0]
    return best


# -----------------------------
# Dynamic workflow: grid + BO + early stopping
# -----------------------------
@dynamic
def tune_dynamic(
    search_space: SearchSpace,
    tcfg: TuningConfig,
    data_cfg: TrainingDataConfig,
) -> Tuple[TrialResult, List[TrialResult]]:
    # Generate / load data once
    X_train, y_train, X_val, y_val = generate_synthetic_data(cfg=data_cfg)

    # Prepare candidates from grid
    grid_candidates = expand_grid(search_space)
    if len(grid_candidates) == 0:
        raise ValueError("Empty search space; provide at least one parameter")

    rng = seeded_random(tcfg.random_seed)
    # Shuffle grid for variety
    rng.shuffle(grid_candidates)

    results: List[TrialResult] = []
    best: Optional[TrialResult] = None
    bo_module = load_bo_suggester(tcfg.bo_callback_module)

    def is_improvement(curr: float, prev: float, mode: str, min_delta: float) -> bool:
        if mode == "max":
            return (curr - prev) > min_delta
        return (prev - curr) > min_delta

    patience_counter = 0

    # Iterate
    max_iters = min(tcfg.max_iterations, len(grid_candidates))
    idx = 0
    while idx < max_iters:
        batch = []
        # Select a batch up to tcfg.parallelism
        for _ in range(min(tcfg.parallelism, max_iters - idx)):
            if idx < tcfg.bo_explore_start:
                cand = grid_candidates[idx]
            else:
                # Bayesian optimization suggestion if available, else fallback to remaining grid
                if bo_module and hasattr(bo_module, "suggest"):
                    try:
                        cand = bo_module.suggest(
                            past_results=[dataclasses.asdict(r) for r in results],
                            search_space=dataclasses.asdict(search_space),
                            rng_seed=rng.randint(0, 10_000_000),
                        )
                        # basic validation of suggestion
                        if not isinstance(cand, dict) or not cand:
                            raise ValueError("Invalid BO suggestion")
                    except Exception as e:
                        logger.warning("BO suggest failed, falling back to grid: %s", e)
                        cand = grid_candidates[idx]
                else:
                    cand = grid_candidates[idx]
            batch.append(cand)
            idx += 1

        # Execute batch in parallel
        batch_results = parallel_eval.map(
            params=batch, X_train=X_train, y_train=y_train, X_val=X_val, y_val=y_val
        )
        # Collect
        for r in batch_results:
            results.append(r)
            if r.status == "ok" and tcfg.early_stopping.monitor in r.metrics:
                if best is None:
                    best = r
                    patience_counter = 0
                else:
                    curr = r.metrics[tcfg.early_stopping.monitor]
                    prev = best.metrics[tcfg.early_stopping.monitor]
                    if is_improvement(curr, prev, tcfg.early_stopping.mode, tcfg.early_stopping.min_delta):
                        best = r
                        patience_counter = 0
                    else:
                        patience_counter += 1

        # Early stop check
        if patience_counter >= tcfg.early_stopping.patience:
            logger.info("Early stopping triggered: patience %d reached", patience_counter)
            break

    if best is None:
        best = select_best(results=results, monitor=tcfg.early_stopping.monitor, mode=tcfg.early_stopping.mode)

    return best, results


# -----------------------------
# Public workflow
# -----------------------------
@workflow
def hyperparameter_tuning_wf(
    # Example RF-oriented search space
    discrete: Dict[str, List[Any]] = {
        "n_estimators": [100, 200, 400],
        "min_samples_split": [2, 4],
        "min_samples_leaf": [1, 2],
        "max_features": ["sqrt", "log2"],
        "random_state": [0, 7, 21],
    },
    continuous: Dict[str, Tuple[float, float, Optional[float]]] = {
        # max_depth as continuous then discretized; use None by leaving out or set via discrete if needed
        "max_depth": (3.0, 15.0, 3.0),
    },
    max_iterations: int = 24,
    parallelism: int = 6,
    patience: int = 5,
    bo_explore_start: int = 8,
    bo_callback_module: Optional[str] = None,
) -> Tuple[TrialResult, List[TrialResult]]:
    search = SearchSpace(discrete=discrete, continuous=continuous)
    tcfg = TuningConfig(
        max_iterations=max_iterations,
        parallelism=parallelism,
        early_stopping=EarlyStoppingConfig(patience=patience),
        bo_explore_start=bo_explore_start,
        bo_callback_module=bo_callback_module,
    )
    data_cfg = TrainingDataConfig()
    return tune_dynamic(search_space=search, tcfg=tcfg, data_cfg=data_cfg)


# -----------------------------
# CLI helper (optional local run)
# -----------------------------
if __name__ == "__main__":
    # Quick local smoke test (non-Flyte execution)
    logging.getLogger().setLevel(logging.INFO)
    best, all_results = hyperparameter_tuning_wf()
    print("Best:", best)
    print("Trials:", len(all_results))
