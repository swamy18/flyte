# Cost Optimization Strategies for Flyte Deployments

This document provides actionable, production-grade practices to minimize cloud costs for Flyte control plane and data-plane workloads while preserving reliability and performance. It covers spot instance usage, autoscaling, resource sizing, caching, workflow optimization, storage lifecycle, and FinOps monitoring, with concrete configurations and calculation examples.

---

## 1) Use Spot/Preemptible Instances Safely

Goal: Run non-latency-critical and fault-tolerant tasks on cheaper spot capacity with automatic retries and interruption resilience.

Recommended mapping
- Control plane: on-demand/standard instances (99.9%+ availability)
- Worker pods (task pods) in user projects: a mix of spot and on‑demand via node pools/ASGs and taints/tolerations
- Databases/queues/object storage: managed services or on-demand only

Kubernetes setup
- Create two node groups:
  - ng-ondemand: regular pricing; label: nodegroup=ondemand
  - ng-spot: spot/preemptible; label: nodegroup=spot; taint: spot=true:NoSchedule
- Pod tolerations/affinity for Flyte tasks (values.yaml for flyte-binary or per‑project plugin config):

```yaml
# Example pod template for user task pods
pod_template:
  tolerations:
    - key: "spot"
      operator: "Equal"
      value: "true"
      effect: "NoSchedule"
  nodeSelector:
    nodegroup: spot
  # Prefer but do not require spot
  affinity:
    nodeAffinity:
      preferredDuringSchedulingIgnoredDuringExecution:
        - weight: 100
          preference:
            matchExpressions:
              - key: nodegroup
                operator: In
                values: ["spot"]
```

Flyte retry policy
- Set max task retries to handle preemptions gracefully:
```yaml
# In Flyte task definition (Python)
@task(retries=3, retry_strategy="RETRY_STRATEGY_EXPONENTIAL_BACKOFF")
def my_task(...):
    ...
```

Interruption handling
- For Spark/Array/Map tasks, enable checkpointing or idempotent outputs.
- Use Flyte’s cache-on-success and durable outputs (see sections below) to avoid recomputation after preemption.

Cost example
- Assume on-demand price = $1.00/hr, spot discount 70% -> $0.30/hr
- With 10k task-hours/month, 80% scheduled to spot: Cost = 0.8*10k*0.30 + 0.2*10k*1.00 = $2,400 + $2,000 = $4,400 vs $10,000 on-demand (56% savings)

---

## 2) Horizontal Pod Autoscaling and Cluster Autoscaling

Goal: Scale workers with demand to avoid idle capacity.

Worker deployment autoscaling
- For flyte-binary or FlytePropeller, keep on-demand stable; scale executors/agent pods.
- Use HPA targeting CPU or concurrency metrics:
```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata: { name: flyte-executors }
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: flyte-executors
  minReplicas: 2
  maxReplicas: 50
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 70
```

Cluster autoscaler
- Enable Cluster Autoscaler to add/remove nodes for spot and on-demand groups.
- Configure scale-down-unneeded-time=10m, scale-down-utilization-threshold=0.5 to cut idle.

Queue-based autoscaling (optional)
- Scale on queue length of workflow executions using KEDA with FlyteAdmin metrics endpoints.

Savings example
- Right-sizing maxReplicas to 50 and enabling CA reduced idle hours by 300 node-hours/week. At $0.30/hr spot = ~$90/week saved.

---

## 3) Resource Request/Limit Optimization

Goal: Right-size CPU/memory/GPU per task to minimize waste and throttling.

Guidelines
- Start with measured baselines from prior runs (Flyte collects task runtime and resource usage via Prometheus).
- Set requests close to p50 usage; set limits ~p95–p99 to prevent OOM/throttle.
- Avoid setting limits without need on CPU-bound tasks to reduce throttling.

Configuration (Python)
```python
from flytekit import task, Resources

@task(requests=Resources(cpu="500m", mem="1Gi"),
      limits=Resources(cpu="1000m", mem="2Gi"))
def transform(...):
    ...
```

Iterative tuning
- Use historical metrics dashboards (see Section 7) to adjust sizes every sprint.
- For Spark/BigQuery/Snowflake tasks, size executors/slots via plugin config.

Calculation example
- Original: requests 2 vCPU/8Gi for 1,000 task-hours/month on on-demand $1.00/hr  (node)
- After tuning to 0.5 vCPU/1Gi on spot $0.30/hr, effective node-hours drop 75% and price 70%: cost ≈ 0.25*0.30/1.00 = 7.5% of baseline → ~92.5% savings for those tasks.

---

## 4) Task Caching and Data Reuse

Goal: Avoid recomputing identical results.

Enable cache
```python
@task(cache=True, cache_version="v1", cache_serialize=True)
def preprocess(df: pd.DataFrame) -> Output[...]:
    ...
```

Best practices
- Increment cache_version only when logic changes.
- Use strong, deterministic inputs and avoid wall-clock dependencies.
- Store outputs in object storage with content-addressable keys if possible.

Durable task outputs
- Use flytepropeller config to write outputs to remote storage so retries do not recompute.
```yaml
propeller:
  workflows:
    defaults:
      maxNodeRetries: 3
  rawoutput-prefix: s3://<bucket>/flyte-raw-outputs/
```

Savings example
- If 40% of daily runs hit cache for a 1-hr task at $0.30/hr, monthly savings for 1,000 runs ≈ 0.4*1000*0.30 = $120.

---

## 5) Workflow Optimization Techniques

Goal: Reduce total compute time and duplicate work.

- Parallelize independent branches using map tasks and dynamic workflows.
- Use small, composable tasks to enable fine-grained cache hits.
- Coalesce tiny tasks to reduce container spin-up overhead when startup time dominates.
- Use retries with backoff to survive transient failures while minimizing repeated heavy work.
- Prefer columnar formats (Parquet) and predicate pushdown.
- Push joins/aggregations down to engines (Spark/BigQuery/Snowflake) where cheaper.

Example: map task batching
```python
@task
def score(batch: list[int]) -> list[float]: ...

@workflow
def scoring(ids: list[int]) -> list[float]:
    batches = [ids[i:i+100] for i in range(0, len(ids), 100)]
    return map_task(score)(batch=batches)
```

---

## 6) Storage Lifecycle and Data Retention

Goal: Lower storage costs for logs, artifacts, and intermediate data.

Object storage policies (S3 example)
- Buckets: flyte-raw-outputs, flyte-artifacts, flyte-logs
- Lifecycle rules:
  - Transition to Infrequent Access after 30 days
  - Transition to Glacier/Deep Archive after 90/180 days
  - Expire intermediates after 30–60 days if reproducible via cache

Example S3 lifecycle JSON (snippet)
```json
{
  "Rules": [
    {"ID": "raw-to-ia", "Filter": {"Prefix": "raw/"}, "Status": "Enabled",
     "Transitions": [{"Days": 30, "StorageClass": "STANDARD_IA"}],
     "Expiration": {"Days": 180}},
    {"ID": "logs-expire", "Filter": {"Prefix": "logs/"}, "Status": "Enabled",
     "Expiration": {"Days": 90}}
  ]
}
```

Logs retention
- Configure Flyte to stream logs to object storage and prune cluster log retention to 7–14 days.

---

## 7) Cost Monitoring and FinOps

Goal: Establish visibility, budgets, and continuous optimization.

Tagging and attribution
- Enforce per-project, per-domain labels to propagate to pods and cloud billing tags.
- Example annotations on task pods:
```yaml
metadata:
  labels:
    flyte-project: payments
    flyte-domain: production
    flyte-workflow: churn_train
```

Metrics and dashboards
- Scrape FlyteAdmin, FlytePropeller, and Kubernetes metrics with Prometheus.
- Key views:
  - Cost per project/domain (using cloud CUR + label joins)
  - Task success rate and retry counts (spot disruption impact)
  - CPU/memory requested vs used (right-sizing opportunities)
  - Cache hit ratio per task/workflow

Budgets and alerts
- Define monthly budgets per project; alert at 50/80/100%.
- Trigger anomaly detection on daily spend spikes >3σ from rolling 14-day mean.

Example CUR-based cost equation
- Daily cost by project = Σ(resource_hours × rate) joined on pod labels
- If payments project used 800 vCPU-hours on spot at $0.03/vCPU-hr and 200 on on-demand at $0.10/vCPU-hr:
  Cost = 800*0.03 + 200*0.10 = $24 + $20 = $44/day

---

## 8) Reference Config Snippets

Flyte Helm values (flyte-binary)
```yaml
config:
  propeller:
    rawoutput-prefix: s3://my-bucket/flyte-raw-outputs/
    workflows:
      defaults:
        maxNodeRetries: 3
  admin:
    metricsScope: flyteadmin
  task_resources:
    defaults:
      cpu: 500m
      memory: 1Gi
    limits:
      cpu: 1000m
      memory: 2Gi

executor:
  hpa:
    enabled: true
    minReplicas: 2
    maxReplicas: 50
    targetCPUUtilizationPercentage: 70

nodeSelector:
  nodegroup: ondemand
```

Pod Priority and Preemption
```yaml
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: flyte-control-critical
value: 1000000
preemptionPolicy: Never
```

---

## 9) Governance and Runbooks

- Quarterly review: right-sizing, cache efficacy, spot coverage, lifecycle policy compliance.
- Incident playbook: spot capacity shortage → temporarily switch nodeSelector to on-demand; scale HPA minReplicas.
- DR plan: keep control plane HA on on-demand; back up Flyte Admin DB daily.

---

## 10) Quick Checklist

- [ ] Spot node group with taint, task tolerations
- [ ] Executor HPA + Cluster Autoscaler configured
- [ ] Resource requests/limits set from metrics
- [ ] Task caching enabled with durable outputs
- [ ] Storage lifecycle rules applied to artifacts/logs
- [ ] Project/domain labels for cost attribution
- [ ] Budgets and alerts in place with dashboards

Appendix: Tooling
- Cloud provider: CUR/BigQuery billing export, Athena, or Cost and Usage Reports
- Open-source: kube-state-metrics, Prometheus, Grafana, Kubecost/OpenCost
- Optional: KEDA for queue scaling, Airflow cost adapters for migration comparisons
