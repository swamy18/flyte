"""
Comprehensive end-to-end MLOps pipeline for Flyte examples.

Features:
- Data ingestion/validation
- Feature engineering
- Train/val/test split
- Training with experiment tracking (MLflow or JSON fallback)
- Evaluation with multiple metrics
- A/B test decisioning
- Canary deploy and rollback
- Monitoring hooks
- Robust error handling
"""
from __future__ import annotations

import json
import math
import random
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from sklearn.datasets import make_classification
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import average_precision_score, f1_score, precision_score, recall_score, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline as SkPipeline
from sklearn.preprocessing import StandardScaler

try:
    import mlflow  # type: ignore
    import mlflow.sklearn  # type: ignore
    MLFLOW_AVAILABLE = True
except Exception:
    MLFLOW_AVAILABLE = False


@dataclass
class PipelineConfig:
    random_seed: int = 42
    test_size: float = 0.2
    val_size: float = 0.2
    experiment_name: str = "flyte-mlops-pipeline"
    artifacts_dir: Path = Path("./artifacts")
    registry_dir: Path = Path("./model_registry")
    canary_traffic: float = 0.1
    performance_drop_threshold: float = 0.02
    ab_min_improvement: float = 0.005


class PipelineError(Exception):
    pass


class DataValidationError(PipelineError):
    pass


class TrainingError(PipelineError):
    pass


class DeploymentError(PipelineError):
    pass


def ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def log(msg: str, **kv):
    rec = {"ts": ts(), "level": "INFO", "msg": msg, **kv}
    print(json.dumps(rec))


def log_error(msg: str, **kv):
    rec = {"ts": ts(), "level": "ERROR", "msg": msg, **kv}
    print(json.dumps(rec), file=sys.stderr)


def ensure_dirs(*dirs: Path):
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)


def ingest_data(n_samples: int = 20000, n_features: int = 20, seed: int = 42) -> Tuple[np.ndarray, np.ndarray]:
    try:
        X, y = make_classification(
            n_samples=n_samples,
            n_features=n_features,
            n_informative=12,
            n_redundant=4,
            n_classes=2,
            weights=[0.6, 0.4],
            class_sep=1.2,
            random_state=seed,
        )
        log("data_ingested", n_samples=n_samples, n_features=n_features)
        return X.astype(np.float32), y.astype(np.int32)
    except Exception as e:
        log_error("data_ingest_failed", error=str(e))
        raise DataValidationError("Failed to ingest data") from e


def validate_data(X: np.ndarray, y: np.ndarray):
    if X is None or y is None:
        raise DataValidationError("Null dataset")
    if len(X) != len(y):
        raise DataValidationError("X and y length mismatch")
    if np.any(~np.isfinite(X)):
        raise DataValidationError("Non-finite values in features")
    if set(np.unique(y)) - {0, 1}:
        raise DataValidationError("Labels must be 0/1")
    log("data_validated", rows=int(len(y)))


def feature_engineer(X: np.ndarray) -> Tuple[np.ndarray, StandardScaler]:
    try:
        scaler = StandardScaler()
        Xs = scaler.fit_transform(X).astype(np.float32)
        log("features_engineered", method="standard_scaler")
        return Xs, scaler
    except Exception as e:
        log_error("feature_engineering_failed", error=str(e))
        raise PipelineError("Feature engineering failed") from e


def split_data(X: np.ndarray, y: np.ndarray, cfg: PipelineConfig):
    try:
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=cfg.test_size, random_state=cfg.random_seed, stratify=y
        )
        X_tr, X_val, y_tr, y_val = train_test_split(
            X_train, y_train, test_size=cfg.val_size, random_state=cfg.random_seed, stratify=y_train
        )
        log("data_split", train=len(y_tr), val=len(y_val), test=len(y_test))
        return X_tr, X_val, X_test, y_tr, y_val, y_test
    except Exception as e:
        log_error("split_failed", error=str(e))
        raise PipelineError("Data split failed") from e


@dataclass
class ModelRun:
    model_type: str
    params: Dict
    metrics: Dict
    run_id: str
    artifact_path: Path


def start_experiment(cfg: PipelineConfig):
    ensure_dirs(cfg.artifacts_dir)
    if MLFLOW_AVAILABLE:
        try:
            mlflow.set_experiment(cfg.experiment_name)
            mlflow.start_run(run_name=f"run-{int(time.time())}")
            log("mlflow_run_started", experiment=cfg.experiment_name)
        except Exception as e:
            log_error("mlflow_start_failed", error=str(e))
    else:
        log("mlflow_unavailable_fallback_json")


def end_experiment():
    if MLFLOW_AVAILABLE:
        try:
            mlflow.end_run()
            log("mlflow_run_ended")
        except Exception as e:
            log_error("mlflow_end_failed", error=str(e))


def log_params_metrics_artifacts(params: Dict, metrics: Dict, artifacts: Dict[str, Path]):
    if MLFLOW_AVAILABLE:
        try:
            for k, v in params.items():
                mlflow.log_param(k, v)
            for k, v in metrics.items():
                mlflow.log_metric(k, float(v))
            for name, p in artifacts.items():
                if p.exists():
                    mlflow.log_artifact(str(p), artifact_path=name)
            log("mlflow_logged")
            return
        except Exception as e:
            log_error("mlflow_log_failed", error=str(e))
    rec = {"params": params, "metrics": {k: float(v) for k, v in metrics.items()}, "artifacts": {k: str(v) for k, v in artifacts.items()}, "ts": ts()}
    ensure_dirs(Path("./runs_json"))
    out = Path("./runs_json") / f"run_{int(time.time())}.json"
    out.write_text(json.dumps(rec, indent=2))
    log("json_logged", path=str(out))


def train_models(X_tr, y_tr, X_val, y_val, cfg: PipelineConfig) -> List[ModelRun]:
    start_experiment(cfg)
    runs: List[ModelRun] = []
    try:
        candidates: List[Tuple[str, object, Dict]] = [
            (
                "logreg",
                SkPipeline([
                    ("scaler", StandardScaler()),
                    ("clf", LogisticRegression(max_iter=1000, random_state=cfg.random_seed)),
                ]),
                {"type": "logreg", "max_iter": 1000},
            ),
            (
                "rf",
                RandomForestClassifier(n_estimators=200, random_state=cfg.random_seed, n_jobs=-1),
                {"type": "rf", "n_estimators": 200},
            ),
        ]
        for name, model, params in candidates:
            model.fit(X_tr, y_tr)
            proba = model.predict_proba(X_val)[:, 1]
            preds = (proba >= 0.5).astype(int)
            metrics = {
                "roc_auc": roc_auc_score(y_val, proba),
                "pr_auc": average_precision_score(y_val, proba),
                "f1": f1_score(y_val, preds),
                "precision": precision_score(y_val, preds, zero_division=0),
                "recall": recall_score(y_val, preds, zero_division=0),
            }
            art_dir = cfg.artifacts_dir / f"{name}_{int(time.time())}"
            ensure_dirs(art_dir)
            np.save(art_dir / "val_proba.npy", proba.astype(np.float32))
            (art_dir / "metrics.json").write_text(json.dumps({k: float(v) for k, v in metrics.items()}, indent=2))
            log_params_metrics_artifacts(params, metrics, {"model": art_dir})
            runs.append(ModelRun(name, params, metrics, run_id=f"{name}-{int(time.time())}", artifact_path=art_dir))
            log("model_trained", model=name, **{k: float(v) for k, v in metrics.items()})
    except Exception as e:
        log_error("training_failed", error=str(e))
        raise TrainingError("Training failed") from e
    finally:
        end_experiment()
    return runs


def pick_best_run(runs: List[ModelRun], metric: str = "roc_auc") -> ModelRun:
    if not runs:
        raise TrainingError("No runs available")
    best = max(runs, key=lambda r: r.metrics.get(metric, float("-inf")))
    log("best_run_selected", model=best.model_type, metric=metric, value=float(best.metrics.get(metric, -1)))
    return best


def ab_test_decision(control: ModelRun, candidate: ModelRun, metric: str, cfg: PipelineConfig) -> bool:
    base_c = float(control.metrics[metric])
    base_n = float(candidate.metrics[metric])
    rng = np.random.default_rng(123)
    ctrl = list(np.clip(rng.normal(loc=base_c, scale=0.02, size=200), 0, 1))
    cand = list(np.clip(rng.normal(loc=base_n, scale=0.02, size=200), 0, 1))
    ctrl_mean, cand_mean = statistics.mean(ctrl), statistics.mean(cand)
    ctrl_var, cand_var = statistics.pvariance(ctrl), statistics.pvariance(cand)
    n1, n2 = len(ctrl), len(cand)
    denom = math.sqrt(ctrl_var / n1 + cand_var / n2 + 1e-12)
    t = (cand_mean - ctrl_mean) / (denom if denom > 0 else 1.0)
    improvement = cand_mean - ctrl_mean
    promote = improvement >= cfg.ab_min_improvement and cand_mean >= ctrl_mean
    log("ab_test_evaluated", ctrl_mean=float(ctrl_mean), cand_mean=float(cand_mean), t=float(t), improvement=float(improvement), decision=bool(promote))
    return promote


def read_metric(model_dir: Path, name: str) -> float:
    try:
        m = json.loads((model_dir / "metrics.json").read_text())
        return float(m.get(name, 0.0))
    except Exception:
        return 0.0


def save_model(run: ModelRun, cfg: PipelineConfig) -> Path:
    ensure_dirs(cfg.registry_dir)
    model_dir = cfg.registry_dir / f"{run.model_type}_{int(time.time())}"
    ensure_dirs(model_dir)
    (model_dir / "params.json").write_text(json.dumps(run.params, indent=2))
    (model_dir / "metrics.json").write_text(json.dumps({k: float(v) for k, v in run.metrics.items()}, indent=2))
    (model_dir / "MODEL.txt").write_text(f"Model: {run.model_type}\nRun: {run.run_id}\n")
    log("model_saved", path=str(model_dir))
    return model_dir


def get_current_production(cfg: PipelineConfig) -> Optional[Path]:
    alias = cfg.registry_dir / "PRODUCTION"
    return alias.resolve() if alias.exists() else None


def point_alias(alias: Path, target: Path):
    if alias.exists() or alias.is_symlink():
        alias.unlink()
    alias.symlink_to(target, target_is_directory=True)


def canary_deploy(control_dir: Path, candidate_dir: Path, cfg: PipelineConfig) -> bool:
    alias_control = cfg.registry_dir / "PRODUCTION"
    alias_canary = cfg.registry_dir / "CANARY"
    try:
        point_alias(alias_control, control_dir)
        point_alias(alias_canary, candidate_dir)
        log("canary_started", traffic=cfg.canary_traffic)
        rng = random.Random(cfg.random_seed)
        control_err = 0.01 + rng.random() * 0.01
        candidate_err = 0.01 + rng.random() * 0.01
        drop = float(read_metric(candidate_dir, "roc_auc")) - float(read_metric(control_dir, "roc_auc"))
        if drop < -cfg.performance_drop_threshold:
            candidate_err += 0.02
        log("monitor_sample", control_err=control_err, candidate_err=candidate_err)
        healthy = candidate_err <= control_err + 0.01
        log("canary_health", healthy=bool(healthy))
        return healthy
    except Exception as e:
        log_error("canary_failed", error=str(e))
        raise DeploymentError("Canary deployment failed") from e


def rollback(cfg: PipelineConfig):
    try:
        alias_canary = cfg.registry_dir / "CANARY"
        if alias_canary.exists() or alias_canary.is_symlink():
            alias_canary.unlink()
        log("rollback_completed")
    except Exception as e:
        log_error("rollback_failed", error=str(e))
        raise DeploymentError("Rollback failed") from e


def emit_monitoring_metrics(stage: str, **kv):
    log("monitoring", stage=stage, **kv)


def main():
    cfg = PipelineConfig()
    ensure_dirs(cfg.artifacts_dir, cfg.registry_dir)

    X, y = ingest_data(seed=cfg.random_seed)
    validate_data(X, y)
    Xs, _ = feature_engineer(X)
    X_tr, X_val, X_test, y_tr, y_val, y_test = split_data(Xs, y, cfg)

    runs = train_models(X_tr, y_tr, X_val, y_val, cfg)
    runs_sorted = sorted(runs, key=lambda r: r.metrics["roc_auc"], reverse=True)

    control_dir = get_current_production(cfg)
    control_run: Optional[ModelRun] = None
    if control_dir is None:
        control_run = runs_sorted[0]
        control_dir = save_model(control_run, cfg)
        point_alias(cfg.registry_dir / "PRODUCTION", control_dir)
        log("bootstrap_prod", model=control_run.model_type)

    candidate_run = runs_sorted[0]
    if control_run is None:
        try:
            ctrl_metrics = json.loads((control_dir / "metrics.json").read_text())
            control_run = ModelRun("prod", {}, ctrl_metrics, run_id="prod", artifact_path=control_dir)
        except Exception:
            control_run = runs_sorted[0]

    if ab_test_decision(control_run, candidate_run, metric="roc_auc", cfg=cfg):
        cand_dir = save_model(candidate_run, cfg)
        healthy = canary_deploy(control_dir, cand_dir, cfg)
        if healthy:
            point_alias(cfg.registry_dir / "PRODUCTION", cand_dir)
            log("promoted_to_production", model=candidate_run.model_type)
        else:
            rollback(cfg)
            log("rollback_triggered")
    else:
        log("ab_test_rejected")

    emit_monitoring_metrics("post_deploy", prod=str(get_current_production(cfg)))


if __name__ == "__main__":
    main()
