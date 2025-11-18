# ML Workflows Examples

This directory contains advanced MLOps workflow examples demonstrating Flyte's capabilities for building production-grade machine learning pipelines.

## 📁 Workflows

### 1. **mlops_pipeline.py** - End-to-End MLOps Pipeline
A comprehensive production-ready ML pipeline featuring:
- **Feature Engineering**: Automated feature transformation and selection
- **Model Training**: Multi-model training with hyperparameter tuning
- **A/B Testing**: Statistical comparison between control and candidate models
- **Canary Deployment**: Progressive rollout with health checks
- **Monitoring**: Post-deployment metrics and alerting
- **Rollback**: Automatic rollback on deployment failures

**Key Features:**
- Bootstrap deployment for first-time model launches
- Production model versioning and registry
- Metrics-driven promotion decisions
- Error handling and logging

### 2. **hyperparameter_tuning.py** - Advanced Hyperparameter Optimization
Demonstrates sophisticated hyperparameter search strategies:
- **Bayesian Optimization**: Efficient search using Gaussian processes
- **Grid/Random Search**: Traditional search methods
- **Early Stopping**: Resource-efficient training
- **Cross-Validation**: Robust model evaluation

### 3. **distributed_training.py** - Distributed Training
Scalable training across multiple nodes:
- **Data Parallelism**: Distribute batches across workers
- **Model Parallelism**: Split large models across devices
- **Fault Tolerance**: Handle node failures gracefully
- **Resource Optimization**: Efficient GPU/CPU utilization

### 4. **data_validation_workflow.py** - Data Quality & Drift Detection
Ensure data reliability with:
- **Great Expectations Integration**: Schema and quality validation
- **Drift Detection**: Statistical tests for data distribution changes
- **Anomaly Detection**: Identify outliers and data issues
- **Data Lineage**: Track data provenance and transformations

### 5. **model_registry_and_promotion.py** - Model Lifecycle Management
Manage model versions through stages:
- **Staging Environment**: Pre-production testing
- **A/B Testing**: Compare model versions
- **Gradual Rollout**: Percentage-based traffic splitting
- **Model Metadata**: Track metrics, lineage, and artifacts

## 🚀 Getting Started

### Prerequisites
```bash
pip install flytekit scikit-learn pandas numpy
```

### Running Workflows Locally

1. **Single Workflow Execution:**
```bash
pyflyte run mlops_pipeline.py main
```

2. **With Flyte Sandbox:**
```bash
# Start local Flyte cluster
flytectl demo start

# Register workflow
pyflyte register mlops_pipeline.py

# Execute remotely
pyflyte run --remote mlops_pipeline.py main
```

### Configuration

Each workflow accepts configuration parameters:
- `random_seed`: Reproducibility seed
- `test_size`: Train/test split ratio
- `primary_metric`: Metric for model comparison (e.g., 'accuracy', 'f1')
- `registry_dir`: Path for model artifacts

## 🛠️ Engineering Best Practices

### Type Safety
All workflows use Python type hints and Flyte's strongly-typed interfaces for:
- Input/output validation
- Compile-time error detection
- Better IDE support

### Modularity
Workflows are decomposed into reusable tasks:
```python
@task
def preprocess_data(df: pd.DataFrame) -> pd.DataFrame:
    # Task-level caching enabled
    return processed_df
```

### Observability
- Structured logging with context
- Metrics emission for monitoring
- Task-level execution tracking

### Error Handling
```python
try:
    result = risky_operation()
except Exception as e:
    log_error("operation_failed", error=str(e))
    # Alert/page operations team
    raise
```

## 📊 Monitoring & Metrics

Workflows emit metrics for:
- **Training Performance**: Loss, accuracy, training time
- **Data Quality**: Validation pass rates, drift scores
- **Deployment Health**: Canary success rates, rollback frequency
- **Resource Usage**: CPU, memory, GPU utilization

## 🧪 Testing

### Unit Tests
```bash
pytest tests/test_ml_workflows.py
```

### Integration Tests
```bash
pyflyte run --test mlops_pipeline.py main
```

## 📚 Additional Resources

- [Flyte Documentation](https://docs.flyte.org)
- [MLOps Best Practices](https://ml-ops.org/)
- [Flyte Examples](https://github.com/flyteorg/flytesnacks)

## 🤝 Contributing

Improvements welcome! Please:
1. Follow Flyte's [contribution guidelines](https://docs.flyte.org/en/latest/community/contribute/index.html)
2. Add tests for new workflows
3. Update this README with new examples

## 📝 License

Apache 2.0 - See LICENSE file for details
