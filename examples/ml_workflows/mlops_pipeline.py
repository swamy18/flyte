        = cfg.random_seed)
    validate_data(X, y)

    # Feature engineering
    Xs, _ = feature_engineer(X)

    # Split
    X_tr, X_val, X_test, y_tr, y_val, y_test = split_data(Xs, y, cfg)

    # Train
    runs = train_models(X_tr, y_tr, X_val, y_val, cfg)
    if not runs:
        raise TrainingError("No runs produced by training")

    # Determine control (current prod) and candidate (best of current runs)
    runs_sorted = sorted(runs, key=lambda r: r.metrics[cfg.primary_metric], reverse=True)
    control_dir = get_current_production(cfg)
    control_run: Optional[ModelRun] = None

    if control_dir is None:
        # Bootstrap: first time deployment
        control_run = runs_sorted[0]
        control_dir = save_model(control_run, cfg)
        point_alias(cfg.registry_dir / "PRODUCTION", control_dir)
        log("bootstrap_prod", model=control_run.model_type)

    candidate_run = runs_sorted[0]

    # If we have a prod model, load its metrics as control
    if control_run is None:
        try:
            ctrl_metrics = json.loads((control_dir / "metrics.json").read_text())
            control_run = ModelRun("prod", {}, ctrl_metrics, run_id="prod", artifact_path=control_dir)
        except Exception:
            control_run = runs_sorted[0]

    # A/B decision
    if ab_test_decision(control_run, candidate_run, metric=cfg.primary_metric, cfg=cfg):
        cand_dir = save_model(candidate_run, cfg)
        # Canary deploy
        healthy = canary_deploy(control_dir, cand_dir, cfg)
        if healthy:
            # Promote to production
            point_alias(cfg.registry_dir / "PRODUCTION", cand_dir)
            log("promoted_to_production", model=candidate_run.model_type)
        else:
            rollback(cfg)
            log("rollback_triggered")
    else:
        log("ab_test_rejected")

    # Post-deploy monitoring hook
    emit_monitoring_metrics("post_deploy", prod=str(get_current_production(cfg)))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_error("pipeline_failed", error=str(e))
        # In production, you could alert/pager here
        raise
