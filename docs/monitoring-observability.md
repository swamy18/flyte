# Monitoring & Observability for Flyte

This document provides end-to-end guidance to instrument, collect, visualize, alert, and operate Flyte using Prometheus, Grafana, centralized logging (ELK/EFK), distributed tracing with Jaeger, and SLA monitoring. It includes concrete queries, alert rules, sample Grafana dashboards (JSON), and operational tips.

## 1. Prometheus Metrics

### 1.1 Components & scrape targets
Scrape the following Flyte services (ports are examples; adjust to your deployment):
- flyteadmin: http://flyteadmin:8088/metrics
- flytepropeller: http://flytepropeller:10254/metrics
- datacatalog: http://datacatalog:8080/metrics
- flyteconsole (optional app metrics): http://flyteconsole:8089/metrics
- envoy / flyte-gateway (if used): http://envoy:9901/stats/prometheus
- Kubernetes kube-state-metrics, node-exporter, cAdvisor for cluster health

Example scrape config:

scrape_configs:
- job_name: flyteadmin
  static_configs:
  - targets: ["flyteadmin:8088"]
  metrics_path: /metrics
- job_name: flytepropeller
  static_configs:
  - targets: ["flytepropeller:10254"]
- job_name: datacatalog
  static_configs:
  - targets: ["datacatalog:8080"]
- job_name: envoy
  metrics_path: /stats/prometheus
  static_configs:
  - targets: ["envoy:9901"]

### 1.2 Key Flyte metrics to monitor
- flyteadmin_request_total{code,method,handler}
- flyteadmin_request_duration_seconds_bucket/sum/count
- flytepropeller_workflow_total{phase}
- flytepropeller_task_execution_total{phase,task_type}
- flytepropeller_enqueue_total / dequeue_total
- flytepropeller_retries_total{reason}
- datacatalog_request_total{code,method}
- go_goroutines, process_resident_memory_bytes, process_cpu_seconds_total
- grpc_server_handled_total{grpc_code,grpc_method,grpc_service}

### 1.3 Useful PromQL queries
- Request rate (RPS):
  sum by(handler) (rate(flyteadmin_request_total[5m]))
- Error rate (%):
  100 * sum(rate(flyteadmin_request_total{code=~"5.."}[5m])) / sum(rate(flyteadmin_request_total[5m]))
- P95 request latency (seconds):
  histogram_quantile(0.95, sum by(le, handler) (rate(flyteadmin_request_duration_seconds_bucket[5m])))
- Active workflows by phase:
  sum by(phase) (rate(flytepropeller_workflow_total[5m]))
- Task failures by type:
  sum by(task_type) (rate(flytepropeller_task_execution_total{phase="FAILED"}[15m]))
- Retries per reason:
  sum by(reason) (increase(flytepropeller_retries_total[1h]))
- Envoy upstream 5xx:
  sum(rate(envoy_cluster_upstream_rq{response_code_class="5"}[5m]))

## 2. Grafana Dashboards

### 2.1 Suggested dashboards
- Flyte Admin API overview: RPS, error rate, P50/P95/P99, top handlers
- Propeller execution: workflow phases, task phases, retries, queue depth, enqueue/dequeue
- Catalog service: latency, hit/miss, error codes
- System health: Go runtime, CPU/memory, pod restarts, K8s saturation

### 2.2 Sample Grafana dashboard JSON (importable)

{
  "title": "Flyte Overview",
  "schemaVersion": 36,
  "version": 1,
  "refresh": "30s",
  "time": {"from": "now-6h", "to": "now"},
  "panels": [
    {
      "type": "stat",
      "title": "Flyte Admin RPS",
      "gridPos": {"x": 0, "y": 0, "w": 6, "h": 4},
      "targets": [{
        "expr": "sum(rate(flyteadmin_request_total[5m]))",
        "legendFormat": "RPS"
      }]
    },
    {
      "type": "stat",
      "title": "Admin Error %",
      "gridPos": {"x": 6, "y": 0, "w": 6, "h": 4},
      "targets": [{
        "expr": "100 * sum(rate(flyteadmin_request_total{code=~\"5..\"}[5m])) / sum(rate(flyteadmin_request_total[5m]))",
        "legendFormat": "error%"
      }],
      "fieldConfig": {"defaults": {"unit": "percent"}}
    },
    {
      "type": "timeseries",
      "title": "Admin P95 latency (s)",
      "gridPos": {"x": 12, "y": 0, "w": 12, "h": 8},
      "targets": [{
        "expr": "histogram_quantile(0.95, sum by(le, handler) (rate(flyteadmin_request_duration_seconds_bucket[5m])))",
        "legendFormat": "{{handler}}"
      }]
    },
    {
      "type": "timeseries",
      "title": "Workflows by phase",
      "gridPos": {"x": 0, "y": 8, "w": 12, "h": 8},
      "targets": [{
        "expr": "sum by(phase) (rate(flytepropeller_workflow_total[5m]))",
        "legendFormat": "{{phase}}"
      }]
    },
    {
      "type": "timeseries",
      "title": "Task failures by type",
      "gridPos": {"x": 12, "y": 8, "w": 12, "h": 8},
      "targets": [{
        "expr": "sum by(task_type) (rate(flytepropeller_task_execution_total{phase=\"FAILED\"}[15m]))",
        "legendFormat": "{{task_type}}"
      }]
    }
  ]
}

Tip: Place this JSON under a Folder "Flyte" in Grafana and parameterize datasource if needed.

## 3. Logging Strategy (ELK/EFK)

### 3.1 Options
- EFK (ElasticSearch + Fluentd/Fluent Bit + Kibana) on Kubernetes
- ELK on VMs with Filebeat/Fluent Bit shipping container logs

### 3.2 Kubernetes EFK quickstart
- Deploy ElasticSearch (or OpenSearch), Kibana
- Deploy Fluent Bit DaemonSet capturing:
  - /var/log/containers/* (with kubernetes metadata filter)
  - Include labels: app, component, namespace, pod, container
- Index strategy per env: flyte-logs-YYYY.MM.DD

Fluent Bit example:

[SERVICE]
  Parsers_File parsers.conf
[INPUT]
  Name tail
  Path /var/log/containers/*flyte*.log
  Tag kube.*
  Parser docker
  Mem_Buf_Limit 50MB
[FILTER]
  Name kubernetes
  Match kube.*
  Kube_URL https://kubernetes.default.svc:443
  Merge_Log On
  Keep_Log Off
[OUTPUT]
  Name es
  Match *
  Host elasticsearch
  Port 9200
  Index flyte-logs
  HTTP_User ${ES_USER}
  HTTP_Passwd ${ES_PASS}

### 3.3 Log fields and queries
Recommended fields: @timestamp, level, message, app, component, workflow, task, execution_id, namespace, pod, container.

Kibana KQL examples:
- Errors last 1h: level:ERROR and app:flyteadmin
- Task failures: component:propeller and message:*FAILED*
- Slow handlers: app:flyteadmin and message:*latency* and level:WARN

## 4. Distributed Tracing with Jaeger

### 4.1 Instrumentation
- Enable OpenTelemetry/Jaeger exporters in Flyte services if available; otherwise, configure Envoy tracing for HTTP/gRPC.
- Propagate context: traceparent (W3C), grpc-trace-bin. Ensure SDKs/tasks propagate headers.

### 4.2 Deployment
- All-in-one for dev: jaegertracing/all-in-one
- Prod: collector + agent + query + storage (Elasticsearch/ClickHouse)
- Envoy example:

tracing:
  http:
    name: envoy.tracers.opencensus
    typed_config:
      "@type": type.googleapis.com/envoy.config.trace.v2.OpenCensusConfig
      trace_config:
        constant_sampler:
          decision: ALWAYS_SAMPLE
      exporters:
        jaeger:
          endpoint: jaeger-collector:14268

### 4.3 Useful trace queries
- service = flyteadmin, operation = grpc.unary, duration > 500ms
- service = flytepropeller, tag execution_id = <id>
- service = envoy, error = true

## 5. Alerting Rules

Create PrometheusRule CRDs (or rules files) and route via Alertmanager.

Example rules:

- Alert: FlyteAdminHighErrorRate
  expr: (sum(rate(flyteadmin_request_total{code=~"5.."}[10m])) / sum(rate(flyteadmin_request_total[10m]))) > 0.05
  for: 10m
  labels: {severity: warning}
  annotations: {summary: "Flyte Admin 5xx >5%", description: "Investigate upstream deps or regressions."}

- Alert: FlyteAdminHighLatencyP95
  expr: histogram_quantile(0.95, sum by(le) (rate(flyteadmin_request_duration_seconds_bucket[10m]))) > 0.5
  for: 15m
  labels: {severity: warning}

- Alert: PropellerTaskFailures
  expr: increase(flytepropeller_task_execution_total{phase="FAILED"}[30m]) > 20
  for: 5m
  labels: {severity: critical}

- Alert: PropellerRetriesSpike
  expr: increase(flytepropeller_retries_total[30m]) > 100
  for: 10m

- Alert: EnvoyUpstream5xx
  expr: sum(rate(envoy_cluster_upstream_rq{response_code_class="5"}[5m])) > 1
  for: 10m

- Alert: PodRestarts
  expr: increase(kube_pod_container_status_restarts_total{namespace=~"flyte.*"}[30m]) > 3
  for: 10m

- Alert: HighMemoryUsage
  expr: (container_memory_working_set_bytes{namespace=~"flyte.*"} / container_spec_memory_limit_bytes{namespace=~"flyte.*"}) > 0.9
  for: 15m

## 6. SLA Monitoring

Define SLOs with SLIs and derive error budgets. Examples:
- Availability SLI (Admin):
  sli = 1 - sum(rate(flyteadmin_request_total{code=~"5.."}[5m])) / sum(rate(flyteadmin_request_total[5m]))
  SLO target: 99.9% monthly
- Latency SLI (P95):
  sli = histogram_quantile(0.95, sum by(le) (rate(flyteadmin_request_duration_seconds_bucket[5m])))
  SLO target: P95 < 400ms
- Workflow success rate:
  sli = 1 - (sum(rate(flytepropeller_task_execution_total{phase="FAILED"}[5m])) / sum(rate(flytepropeller_task_execution_total[5m])))
  SLO target: 99%

Error budget burn alerts (multiwindow, multiburn):
- Fast burn (2h, 14.4x):
  expr: (1 - sli_window_2h) > (1 - SLO) * 14.4
- Slow burn (6h, 6x):
  expr: (1 - sli_window_6h) > (1 - SLO) * 6

Grafana burn rate panels can be built using recording rules for sli_window_*.

## 7. Operational Tips
- Tag metrics and logs with project, domain, workflow, task, execution_id for drill-down.
- Keep metric cardinality under control; bound label values.
- Set retention: metrics 15-30d, logs 7-30d, traces 3-7d (prod depends on storage).
- Use exemplars to link metrics timeseries with traces (OpenTelemetry + Prometheus/Grafana Tempo/Jaeger).
- Regularly run dashboards in CI to detect panel breakage on schema changes.

## 8. References
- Flyte docs: https://docs.flyte.org/
- Prometheus docs: https://prometheus.io/docs/
- Grafana: https://grafana.com/docs/
- Fluent Bit: https://docs.fluentbit.io/
- Jaeger: https://www.jaegertracing.io/docs/
