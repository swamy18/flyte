# Flyte Security Hardening Guide

This guide provides actionable steps and concrete examples to harden a Flyte deployment across networking, identity, secrets, runtime, supply chain, observability, and compliance. Tailor settings to your environment and run changes through staging before production.

## Network Segmentation and Service Mesh

Objectives:
- Enforce zero-trust, mTLS, and least-privilege network paths between Flyte components (console, admin, propeller, data-plane pods, user workloads) and dependencies (Kubernetes API, databases, object stores).
- Apply L7 policies, retries, timeouts, and circuit breaking.

Recommended meshes: Istio or Linkerd.

Example (Istio PeerAuthentication + DestinationRule + AuthorizationPolicy):
```yaml
# mTLS STRICT for namespace flyte
apiVersion: security.istio.io/v1beta1
kind: PeerAuthentication
metadata:
  name: default
  namespace: flyte
spec:
  mtls:
    mode: STRICT
---
# Enforce TLS and set circuit breaking for flyteadmin
apiVersion: networking.istio.io/v1beta1
kind: DestinationRule
metadata:
  name: flyteadmin-dr
  namespace: flyte
spec:
  host: flyteadmin.flyte.svc.cluster.local
  trafficPolicy:
    tls:
      mode: ISTIO_MUTUAL
    connectionPool:
      tcp:
        maxConnections: 100
      http:
        http1MaxPendingRequests: 1000
        maxRequestsPerConnection: 100
    outlierDetection:
      consecutive5xxErrors: 5
      interval: 5s
      baseEjectionTime: 3m
---
# Least-privilege access to Admin only from Propeller and Console
apiVersion: security.istio.io/v1beta1
kind: AuthorizationPolicy
metadata:
  name: flyteadmin-authz
  namespace: flyte
spec:
  selector:
    matchLabels:
      app: flyteadmin
  rules:
  - from:
    - source:
        principals:
        - cluster.local/ns/flyte/sa/flytepropeller
        - cluster.local/ns/flyte/sa/flyteconsole
```

Kubernetes NetworkPolicies (if no mesh):
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-propeller-to-admin
  namespace: flyte
spec:
  podSelector:
    matchLabels:
      app: flyteadmin
  policyTypes: [Ingress]
  ingress:
  - from:
    - podSelector:
        matchLabels:
          app: flytepropeller
    ports:
    - protocol: TCP
      port: 8080
```

## Secrets Management (Vault / AWS Secrets Manager)

Principles:
- Never bake secrets into images or ConfigMaps.
- Use short-lived, auditable credentials and IRSA/KSA binding.

AWS Secrets Manager with IRSA example:
1) Create IAM role and policy for the service account:
```hcl
# Terraform snippet
resource "aws_iam_role" "flyte_sa" {
  name = "flyte-admin-secrets"
  assume_role_policy = data.aws_iam_policy_document.irsa_assume.json
}

resource "aws_iam_role_policy" "secrets_access" {
  role = aws_iam_role.flyte_sa.id
  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action = ["secretsmanager:GetSecretValue"],
      Effect = "Allow",
      Resource = "arn:aws:secretsmanager:REGION:ACCOUNT:secret:flyte/*"
    }]
  })
}
```
2) Bind KSA to IAM role (IRSA):
```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: flyteadmin
  namespace: flyte
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::ACCOUNT:role/flyte-admin-secrets
```
3) Mount secret via sidecar/init or fetch at runtime:
```yaml
containers:
- name: flyteadmin
  image: ghcr.io/flyteorg/flyteadmin:latest
  env:
  - name: DB_PASSWORD
    valueFrom:
      secretKeyRef:
        name: db-password
        key: password
# A controller (external-secrets) syncs from AWS SM -> K8s Secret
```

HashiCorp Vault with Kubernetes auth:
```hcl
# Enable Kubernetes auth in Vault and map SA to a Vault role
vault write auth/kubernetes/config \
  kubernetes_host=https://$K8S_HOST \
  kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt

vault write auth/kubernetes/role/flyteadmin \
  bound_service_account_names=flyteadmin \
  bound_service_account_namespaces=flyte \
  policies=flyte-db \
  ttl=15m
```
Pod template to fetch secret at start:
```yaml
initContainers:
- name: fetch-db-pass
  image: hashicorp/vault:1.15
  args: ["agent", "-config=/vault/config/config.hcl"]
  env:
  - name: VAULT_ADDR
    value: https://vault.example.com
  - name: VAULT_ROLE
    value: flyteadmin
```

## Pod Security (PSA/PSP-equivalent) and Runtime Hardening

Adopt Kubernetes Pod Security Admission (baseline/restricted) with namespace labels:
```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: flyte
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

Baseline container security:
- runAsNonRoot: true, readOnlyRootFilesystem: true
- drop all capabilities; add only what’s required
- seccompProfile: RuntimeDefault
- disallow privilege escalation

Example pod template:
```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
containers:
- name: flytepropeller
  securityContext:
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities:
      drop: ["ALL"]
    seccompProfile:
      type: RuntimeDefault
```

## Image Scanning and Supply Chain Security

- Use a private registry with content trust (Cosign/Sigstore) and enforce imagePolicyWebhook or Kyverno policies.
- Scan images in CI with Trivy/Grype; block critical vulnerabilities.
- Pin digests, not mutable tags.

Kyverno policy to require signatures:
```yaml
apiVersion: kyverno.io/v1
kind: ClusterPolicy
metadata:
  name: require-signed-images
spec:
  validationFailureAction: Enforce
  rules:
  - name: verify-signature
    match:
      any:
      - resources:
          kinds: [Pod]
    verifyImages:
    - imageReferences:
      - "ghcr.io/flyteorg/*"
      attestors:
      - entries:
        - keys:
            publicKeys: |
              -----BEGIN PUBLIC KEY-----
              ...
              -----END PUBLIC KEY-----
```

Trivy in CI example (GitHub Actions):
```yaml
name: image-scan
on: [push, pull_request]
jobs:
  trivy:
    runs-on: ubuntu-latest
    steps:
    - uses: actions/checkout@v4
    - uses: aquasecurity/trivy-action@0.24.0
      with:
        image-ref: ghcr.io/flyteorg/flyteadmin:latest
        format: table
        exit-code: "1"
        ignore-unfixed: true
        vuln-type: os,library
        severity: CRITICAL,HIGH
```

## RBAC Best Practices

- Separate control-plane (admin/propeller/console) and user workload namespaces.
- Use least privilege and bind Roles to ServiceAccounts, not default.
- Avoid wildcards in verbs/resources; prefer resourceNames.

Example Role for Propeller to manage only Flyte CRDs:
```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  namespace: flyte
  name: propeller-role
rules:
- apiGroups: ["flyte.lyft.com"]
  resources: ["workflows", "tasks", "launchplans"]
  verbs: ["get", "list", "watch", "create", "update", "patch", "delete"]
---
kind: RoleBinding
apiVersion: rbac.authorization.k8s.io/v1
metadata:
  name: propeller-binding
  namespace: flyte
subjects:
- kind: ServiceAccount
  name: flytepropeller
  namespace: flyte
roleRef:
  kind: Role
  name: propeller-role
  apiGroup: rbac.authorization.k8s.io
```

## Audit Logging

- Enable Kubernetes audit logging at the API server; ship to a SIEM (e.g., OpenSearch, Splunk).
- Enable Flyte Admin request/response/decision logs with PII redaction.
- Capture mesh (Envoy) access logs.

Kubernetes API server audit policy:
```yaml
apiVersion: audit.k8s.io/v1
kind: Policy
rules:
- level: Metadata
  verbs: ["get", "list", "watch"]
- level: RequestResponse
  resources:
  - group: ""
    resources: ["secrets"]
  - group: "rbac.authorization.k8s.io"
    resources: ["rolebindings", "clusterrolebindings"]
```

Envoy access log example (Istio):
```yaml
meshConfig:
  accessLogFile: /dev/stdout
  accessLogFormat: json
```

## Encryption In Transit and At Rest

- Enforce TLS 1.2+ everywhere; use mesh mTLS or ingress TLS.
- Encrypt object store buckets (S3/GCS/Azure) and databases with CMK/KMS.
- Use envelope encryption for secrets.

AWS S3 bucket policy and default encryption:
```hcl
resource "aws_s3_bucket" "flyte_data" { bucket = "flyte-prod-data" }
resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.flyte_data.id
  rule { apply_server_side_encryption_by_default { sse_algorithm = "aws:kms" kms_master_key_id = aws_kms_key.flyte.arn } }
}
```

TLS for ingress (example NGINX Ingress + cert-manager):
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: flyte-console
  namespace: flyte
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt
spec:
  tls:
  - hosts: ["flyte.example.com"]
    secretName: flyte-console-tls
  rules:
  - host: flyte.example.com
    http:
      paths:
      - path: /
        pathType: Prefix
        backend:
          service:
            name: flyteconsole
            port:
              number: 80
```

## Vulnerability Management

- Schedule routine scans of clusters (Kube-bench, Kube-hunter) and nodes (CIS benchmarks).
- Track CVEs for dependencies; patch weekly or on critical disclosures.
- Use admission control to block known-bad images and privilege escalations.

Example: Gatekeeper policy to prevent privileged pods:
```yaml
apiVersion: templates.gatekeeper.sh/v1beta1
kind: ConstraintTemplate
metadata:
  name: k8spspprivilegedcontainer
spec:
  crd:
    spec:
      names:
        kind: K8sPSPPrivilegedContainer
  targets:
  - target: admission.k8s.gatekeeper.sh
    rego: |
      package k8spspprivilegedcontainer
      violation[{
        "msg": msg,
        "details": {}}] {
        c := input.review.object
        c.kind == "Pod"
        some i
        c.spec.containers[i].securityContext.privileged
        msg := sprintf("Privileged container is not allowed: %v", [c.spec.containers[i].name])
      }
---
apiVersion: constraints.gatekeeper.sh/v1beta1
kind: K8sPSPPrivilegedContainer
metadata:
  name: disallow-privileged
spec: {}
```

## Compliance Framework Mapping

- Map controls to frameworks: CIS Kubernetes, SOC 2, ISO 27001, NIST 800-53, PCI DSS (if applicable).
- Maintain evidence: IaC configs, CI logs, scan reports, change management tickets.

Example control mapping excerpt:
- CIS 5.2.5 (Ensure that the --read-only-port is not set): managed by cluster baseline
- CIS 5.2.8 (Enable audit logging): Kubernetes audit policy configured
- NIST AU-2/6: Audit policy + SIEM shipping
- ISO A.9 (Access control): RBAC least privilege, SSO/OIDC for Flyte Console

## Operational Runbook Checks

- Pre-deploy checklist: image signatures verified, scans clean (no High/Critical), manifests pass policy, secrets present, migrations tested.
- Post-deploy: canary health, error budgets, SLOs, alerting on authz/authn errors and 5xx spikes.
- Quarterly: disaster recovery test, backup restore of metadata DB and object store.

## References
- Flyte docs: https://docs.flyte.org/
- Istio security: https://istio.io/latest/docs/tasks/security/
- Kyverno policies: https://kyverno.io/policies/
- Trivy: https://github.com/aquasecurity/trivy
- Gatekeeper: https://github.com/open-policy-agent/gatekeeper
```
