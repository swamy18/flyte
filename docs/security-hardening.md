# Flyte Security Hardening Guide

This document provides prescriptive guidance and implementation examples to harden a Flyte deployment across Kubernetes clusters. It covers: network segmentation and service mesh, secrets management with external providers, Pod security and isolation, container image and vulnerability scanning, RBAC best practices, audit logging, encryption at rest and in transit, and compliance alignment.

## 1) Network Segmentation with a Service Mesh

Objectives: enforce zero-trust, least-privilege east-west communication, mutual TLS, traffic policies, and observability.

Recommended meshes: Istio (Ambient or sidecar), Linkerd.

Example (Istio):
- Enable strict mTLS cluster-wide:
  ```yaml
  apiVersion: security.istio.io/v1beta1
  kind: PeerAuthentication
  metadata:
    name: default
    namespace: istio-system
  spec:
    mtls:
      mode: STRICT
  ```
- Lock down namespace-to-namespace access using AuthorizationPolicy:
  ```yaml
  apiVersion: security.istio.io/v1beta1
  kind: AuthorizationPolicy
  metadata:
    name: allow-flyte-components
    namespace: flyte
  spec:
    selector:
      matchLabels:
        app.kubernetes.io/part-of: flyte
    rules:
    - from:
      - source:
          namespaces: ["flyte"]
  ```
- Explicit service-to-service policy (only allow FlyteAdmin -> FlytePropeller):
  ```yaml
  apiVersion: security.istio.io/v1beta1
  kind: AuthorizationPolicy
  metadata:
    name: admin-to-propeller
    namespace: flyte
  spec:
    selector:
      matchLabels:
        app.kubernetes.io/name: flytepropeller
    rules:
    - from:
      - source:
          principals: ["cluster.local/ns/flyte/sa/flyteadmin"]
  ```
- Default deny for other traffic:
  ```yaml
  apiVersion: security.istio.io/v1beta1
  kind: AuthorizationPolicy
  metadata:
    name: default-deny
    namespace: flyte
  spec: {}
  ```

Also enforce Kubernetes NetworkPolicies even with a mesh:
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: flyte
spec:
  podSelector: {}
  policyTypes: ["Ingress","Egress"]
```

## 2) Secrets Management with External Providers (Vault, AWS Secrets Manager)

Principles: never bake secrets into images; short-lived credentials; access via workload identity.

- Vault via CSI driver:
  ```yaml
  apiVersion: storage.k8s.io/v1
  kind: CSIDriver
  metadata:
    name: secrets-store.csi.k8s.io
  ---
  apiVersion: secrets-store.csi.x-k8s.io/v1
  kind: SecretProviderClass
  metadata:
    name: flyte-vault-secrets
    namespace: flyte
  spec:
    provider: vault
    parameters:
      roleName: flyte-workload
      objects: |
        - objectName: db-password
          secretPath: kv/data/flyte/db
          secretKey: password
  ```
  Mount in Flyte deployment (example FlyteAdmin):
  ```yaml
  volumeMounts:
  - name: secrets-store
    mountPath: /mnt/secrets
    readOnly: true
  volumes:
  - name: secrets-store
    csi:
      driver: secrets-store.csi.k8s.io
      readOnly: true
      volumeAttributes:
        secretProviderClass: flyte-vault-secrets
  ```
- AWS Secrets Manager using IRSA (EKS):
  - Annotate ServiceAccount with IAM role:
    ```yaml
    apiVersion: v1
    kind: ServiceAccount
    metadata:
      name: flyteadmin
      namespace: flyte
      annotations:
        eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/flyteadmin-secrets
    ```
  - App fetches secrets via SDK at runtime; restrict IAM policy to specific ARNs.

Rotate secrets regularly; enable audit on access in Vault/CloudTrail.

## 3) Pod Security and Isolation

- Enforce Pod Security Standards (restricted level) via namespace labels or PSA admission:
  ```yaml
  apiVersion: v1
  kind: Namespace
  metadata:
    name: flyte
    labels:
      pod-security.kubernetes.io/enforce: "restricted"
      pod-security.kubernetes.io/enforce-version: "latest"
  ```
- Pod-level hardening for all Flyte components and user pods:
  ```yaml
  securityContext:
    runAsNonRoot: true
    allowPrivilegeEscalation: false
    readOnlyRootFilesystem: true
    capabilities:
      drop: ["ALL"]
  ```
- Use per-tenant Kubernetes namespaces and separate node pools for user workloads.
- Apply resource quotas, limitRanges, and seccomp/apparmor profiles.

## 4) Container Image Hygiene and Scanning

- Use a private registry with signed images (Sigstore/Cosign):
  ```bash
  cosign sign ghcr.io/your-org/flyteadmin:TAG
  cosign verify ghcr.io/your-org/flyteadmin:TAG
  ```
- Enforce admission with image policy webhook (Kyverno or OPA Gatekeeper):
  ```yaml
  apiVersion: kyverno.io/v1
  kind: ClusterPolicy
  metadata:
    name: verify-image-signatures
  spec:
    validationFailureAction: enforce
    rules:
    - name: check-signatures
      match:
        any:
        - resources:
            kinds: [Pod]
            namespaces: ["flyte"]
      verifyImages:
      - imageReferences: ["ghcr.io/your-org/*"]
        attestors:
        - entries:
          - keys:
              publicKeys: |
                -----BEGIN PUBLIC KEY-----
                MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A...
                -----END PUBLIC KEY-----
  ```
- Scan images in CI with Trivy/Grype and fail builds on critical CVEs.

## 5) RBAC Best Practices

- Namespace-scope roles for components; avoid cluster-admin.
- Distinct ServiceAccounts per component (flyteadmin, flytepropeller, datacatalog, etc.).
- Example least-privilege Role for FlytePropeller:
  ```yaml
  apiVersion: rbac.authorization.k8s.io/v1
  kind: Role
  metadata:
    name: flytepropeller
    namespace: flyte
  rules:
  - apiGroups: ["", "batch"]
    resources: ["pods","pods/log","configmaps","secrets","events","jobs"]
    verbs: ["get","list","watch","create","update","patch","delete"]
  ---
  kind: RoleBinding
  apiVersion: rbac.authorization.k8s.io/v1
  metadata:
    name: flytepropeller-binding
    namespace: flyte
  subjects:
  - kind: ServiceAccount
    name: flytepropeller
    namespace: flyte
  roleRef:
    kind: Role
    name: flytepropeller
    apiGroup: rbac.authorization.k8s.io
  ```
- Use Kubernetes authz audit to review permissions; periodically run rbac-police/ksniff equivalents.

## 6) Audit Logging

- Enable Kubernetes API audit logging (control plane managed offering dependent).
- For Flyte services, forward application logs to a SIEM (Loki/ELK) with structured fields (request_id, principal, workflow, project, domain).
- Istio mesh: enable access logs and telemetry v2.

Example Fluent Bit output to OpenSearch:
```yaml
[OUTPUT]
    Name  es
    Match kube.*
    Host  opensearch.logging.svc
    Port  9200
    Index flyte-logs
```

## 7) Encryption In Transit and At Rest

- In transit: Use mesh mTLS (STRICT) and TLS for north-south (Ingress/Gateway). Use cert-manager + ACME/private CA.
- At rest: Enable encryption for backing stores:
  - S3/GCS/Azure Blob: SSE-KMS/ CMEK
  - RDS/Postgres: storage encryption + TLS client auth
  - MinIO: KMS-backed SSE-S3 and rotate keys

Ingress example (cert-manager + Gateway API + Istio):
```yaml
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: flyte-cert
  namespace: flyte
spec:
  secretName: flyte-tls
  issuerRef:
    name: letsencrypt-prod
    kind: ClusterIssuer
  dnsNames: ["flyte.example.com"]
```

## 8) Vulnerability and Configuration Scanning

- Cluster: kube-bench (CIS), kube-hunter; enable managed provider security insights.
- Manifests/Policies: Checkov, Polaris, Kubesec.
- Runtime: Falco for syscall detection; Kyverno policies for drift.
- CI/CD: Trivy/Grype/Snyk scans; fail on HIGH/CRITICAL with allowlist for false positives.

## 9) Compliance Frameworks Mapping

- Map controls to CIS Kubernetes, SOC 2, ISO 27001, HIPAA.
- Examples:
  - CIS 5.1 (RBAC) -> Section 5
  - CIS 3.x (Network Policies) -> Section 1
  - SOC 2 CC6.6 (Change Mgmt) -> CI policy checks in Sections 4 & 8
  - HIPAA 164.312 (Transmission Security) -> Section 7 mTLS/TLS

## 10) Operational Practices

- Separate prod/stage/dev clusters; per-tenant projects/domains in Flyte.
- Backup/DR: scheduled DB and object store backups; test restores quarterly.
- Incident response playbooks; chaos testing for security controls.
- Regular pen-testing and threat modeling of data flows.

---

References:
- Flyte deployment: https://docs.flyte.org/en/latest/deployment/index.html
- Istio Security: https://istio.io/latest/docs/tasks/security/
- Linkerd Policy: https://linkerd.io/2.15/features/policy/
- Vault on K8s: https://developer.hashicorp.com/vault/docs/platform/k8s
- Secrets Store CSI: https://secrets-store-csi-driver.sigs.k8s.io/
- Kyverno policies: https://kyverno.io/policies/
- Trivy: https://aquasecurity.github.io/trivy/
