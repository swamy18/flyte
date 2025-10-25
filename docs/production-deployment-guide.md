# Production Deployment Guide for Flyte

This guide provides comprehensive instructions for deploying Flyte in production environments, covering best practices, configuration options, and troubleshooting techniques.

## Prerequisites

Before deploying Flyte in production, ensure you have:

- Kubernetes cluster (v1.21 or later)
- kubectl configured with cluster access
- Helm 3.x installed
- Persistent storage solution (e.g., AWS EBS, GCP PD, Azure Disk)
- Database backend (PostgreSQL 12+ or MySQL 8+)
- Object storage (S3, GCS, or Azure Blob Storage)

## Infrastructure Setup

### Database Configuration

Flyte requires a relational database for metadata storage. For production deployments:

```yaml
database:
  host: your-postgres-instance.region.rds.amazonaws.com
  port: 5432
  dbname: flyte_production
  username: flyte_admin
  passwordPath: /etc/secrets/db-pass
  options: "sslmode=require"
```

**Best Practices:**
- Enable automated backups with point-in-time recovery
- Use read replicas for high-availability deployments
- Configure connection pooling (recommended: 50-100 connections)
- Enable query logging for debugging

### Object Storage Setup

Configure object storage for artifacts, logs, and intermediate data:

```yaml
storage:
  type: s3
  connection:
    region: us-west-2
    auth-type: iam
  container: flyte-production-artifacts
  enable-multicontainer: true
```

### Resource Allocation

Recommended resource allocation for production workloads:

**FlyteAdmin:**
```yaml
resources:
  requests:
    cpu: 1000m
    memory: 2Gi
  limits:
    cpu: 2000m
    memory: 4Gi
replicas: 3
```

**FlytePropeller:**
```yaml
resources:
  requests:
    cpu: 500m
    memory: 1Gi
  limits:
    cpu: 1000m
    memory: 2Gi
replicas: 2
```

## Deployment Steps

### 1. Install Flyte using Helm

```bash
# Add Flyte Helm repository
helm repo add flyteorg https://flyteorg.github.io/flyte
helm repo update

# Create namespace
kubectl create namespace flyte

# Install Flyte with production values
helm install flyte flyteorg/flyte \
  --namespace flyte \
  --values production-values.yaml \
  --set flyteadmin.replicaCount=3 \
  --set flytepropeller.replicaCount=2
```

### 2. Configure TLS/SSL

Secure your Flyte deployment with TLS certificates:

```yaml
ingress:
  enabled: true
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
    kubernetes.io/ingress.class: nginx
  tls:
    - secretName: flyte-tls
      hosts:
        - flyte.yourdomain.com
```

### 3. Enable Authentication

Configure OAuth2/OIDC for production:

```yaml
authentication:
  enabled: true
  oidc:
    baseUrl: https://your-idp.com
    clientId: flyte-production
    clientSecretName: oidc-client-secret
```

## Monitoring and Observability

### Prometheus Metrics

Flyte exports comprehensive metrics for monitoring:

```yaml
prometheus:
  enabled: true
  serviceMonitor:
    enabled: true
    interval: 30s
```

Key metrics to monitor:
- `flyte_propeller_workflow_success_total`
- `flyte_propeller_workflow_failure_total`
- `flyte_admin_api_latency_seconds`
- `flyte_datacatalog_request_duration_seconds`

### Logging Configuration

Configure centralized logging:

```yaml
logging:
  level: info
  output: json
  plugins:
    kubernetes:
      enabled: true
      inject-execution-id: true
```

## Troubleshooting Common Issues

### Issue 1: Workflow Execution Stuck

**Symptoms:** Workflows remain in RUNNING state indefinitely

**Solutions:**
1. Check FlytePropeller logs: `kubectl logs -n flyte deployment/flytepropeller`
2. Verify resource quotas are not exceeded
3. Ensure worker pods have sufficient resources
4. Check for networking issues between components

### Issue 2: High API Latency

**Symptoms:** Slow response times from FlyteAdmin

**Solutions:**
1. Scale up FlyteAdmin replicas
2. Optimize database queries (add indexes)
3. Implement caching layer
4. Review database connection pool settings

### Issue 3: Pod Scheduling Failures

**Symptoms:** Tasks fail with pod scheduling errors

**Solutions:**
1. Verify node resources: `kubectl describe nodes`
2. Check resource quotas: `kubectl describe resourcequota -n flyte`
3. Review PodDisruptionBudgets
4. Ensure proper node affinity/tolerations

## Performance Optimization

### Task Caching

Enable task caching for improved performance:

```python
@task(cache=True, cache_version="1.0")
def expensive_computation(data: pd.DataFrame) -> pd.DataFrame:
    # Task implementation
    return processed_data
```

### Batch Processing

Optimize for large-scale batch workflows:

```yaml
propeller:
  max-workflow-retries: 3
  workers: 40
  workflow-reeval-duration: 10s
  downstream-eval-duration: 5s
```

## Security Hardening

### Network Policies

Implement network segmentation:

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: flyte-network-policy
spec:
  podSelector:
    matchLabels:
      app: flyteadmin
  ingress:
    - from:
      - podSelector:
          matchLabels:
            app: flytepropeller
```

### RBAC Configuration

Configure granular access controls:

```yaml
rbac:
  enabled: true
  serviceAccount:
    create: true
    annotations:
      eks.amazonaws.com/role-arn: arn:aws:iam::account:role/flyte-role
```

## Backup and Disaster Recovery

### Database Backups

```bash
# Automated backup script
#!/bin/bash
DATE=$(date +%Y%m%d_%H%M%S)
pg_dump -h $DB_HOST -U $DB_USER $DB_NAME > flyte_backup_$DATE.sql
aws s3 cp flyte_backup_$DATE.sql s3://backups/flyte/
```

### Configuration Backups

```bash
# Backup Helm values and k8s resources
helm get values flyte -n flyte > flyte-values-backup.yaml
kubectl get all -n flyte -o yaml > flyte-resources-backup.yaml
```

## Scaling Strategies

### Horizontal Pod Autoscaling

```yaml
autoscaling:
  enabled: true
  minReplicas: 3
  maxReplicas: 10
  targetCPUUtilizationPercentage: 70
  targetMemoryUtilizationPercentage: 80
```

### Node Scaling

Configure cluster autoscaler for dynamic node provisioning based on workload demands.

## Production Checklist

- [ ] Database backups configured and tested
- [ ] TLS certificates installed and valid
- [ ] Authentication and authorization enabled
- [ ] Monitoring and alerting configured
- [ ] Resource limits and quotas set
- [ ] Network policies implemented
- [ ] Disaster recovery plan documented
- [ ] High availability setup validated
- [ ] Security scanning completed
- [ ] Performance testing conducted

## Support and Resources

For additional assistance:
- Flyte Community Slack: https://slack.flyte.org
- GitHub Discussions: https://github.com/flyteorg/flyte/discussions
- Documentation: https://docs.flyte.org

## Conclusion

Following this guide will help you deploy and maintain a robust, scalable Flyte production environment. Regularly review and update your configuration as your workload requirements evolve.
