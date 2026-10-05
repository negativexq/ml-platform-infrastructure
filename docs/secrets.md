# Project secrets

Project admins manage credentials through **Project → Settings → Secrets**, or the API
below. Production stores values in Kubernetes Secrets in the project's owned namespace.
PostgreSQL stores workload references and audit metadata, not secret values. Demo mode
uses an in-memory provider and loses values on restart.

This adds project credential management to the existing infrastructure. AWS Terraform
already defines RDS-managed master passwords in Secrets Manager and MLflow IAM access;
that configuration has not been applied. The project feature uses Kubernetes, with a
provider port for a future external backend. There is no Vault, automatic scheduled
rotation, AWS synchronization or historical secret-value recovery in this implementation.

## API and access

All secret-management routes require a project admin or platform admin and a READY
project. The names/keys/version response contains no values, including after creation
and rotation. Backend and validation errors do not echo submitted values.

| Method and path | Behavior |
| --- | --- |
| `GET /projects/{project}/secrets` | List owned names, key names, type, current version and referencing definitions/revisions |
| `POST /projects/{project}/secrets/{name}` | Create; existing names conflict |
| `PUT /projects/{project}/secrets/{name}` | Replace values using `expected_version` |
| `DELETE /projects/{project}/secrets/{name}?expected_version=…` | Delete using the current version; referenced secrets are protected |

Create request (placeholder values):

```json
{
  "kind": "Opaque",
  "values": {"password": "REPLACE_WITH_CREDENTIAL"}
}
```

Rotation submits all new values plus `expected_version` from the list/create response.
It must preserve the existing type and keys. Kubernetes resource-version preconditions
reject concurrent rotation/deletion with 409; refresh metadata before retrying.
Secrets accept 1–64 keys and at most 64 KiB of key/value bytes. Registry secrets use type
`kubernetes.io/dockerconfigjson` and a single `.dockerconfigjson` key whose string value
is a JSON Docker config containing a nonempty `auths` object.

There is no plaintext read endpoint. Write/delete intent is audited before the provider
mutation, and completion afterwards; events contain only name/version and actor. The DB
and Kubernetes mutation are not one transaction: a failure can leave an intent without
a completion event. Refresh provider metadata to determine the actual result.

The API checks namespace ownership and secret ownership labels; it neither adopts nor
overwrites foreign secrets. Its Kubernetes service account has cluster-wide secret CRUD
permissions, while the gateway and reconciler do not receive secret CRUD permissions.
Application ownership checks therefore remain a security boundary for the management API.
Project operators can create workloads that reference secrets, and can consequently
read those credentials from their own running code. Admin-only secret management does
not isolate credentials from people authorized to execute arbitrary project workloads.

## Workload references

Job and model registration accept `secret_refs`:

```json
{
  "secret_refs": {
    "env": {"DB_PASSWORD": {"name": "training-credentials", "key": "password"}},
    "image_pull_secrets": ["private-registry"]
  }
}
```

The registration forms provide secret/key selectors and registry-secret checkboxes.
Operators obtain this metadata through `GET /projects/{project}/secret-references`;
viewers/invokers cannot access the catalog. Management routes remain admin-only.
The API still accepts the JSON structure above. References are checked for ownership,
key existence and registry type at registration. A name cannot appear in both plaintext
`env` and secret `env`; platform-owned environment names cannot be overridden.

Jobs compile environment references to Argo `secretKeyRef`. Pipeline steps inherit their
job references and the workflow combines registry secret names. Models use KServe
predictor environment references and `imagePullSecrets`; deployments snapshot reference
names into immutable revisions for rollback. An explicit LLM `HF_TOKEN` reference
overrides the legacy optional `mlp-hf-token/token` convention. Classic model runtime
environment references do not configure KServe's separate storage initializer; artifact
download authentication still needs its service-account/provider configuration.

Rotation changes the named secret's values, including what old revisions use on future
starts. Running containers retain their existing environment; restart/redeploy them to
consume new credentials. Kubernetes registry pulls also depend on node credential/image
caching. Rotation does not automatically restart workloads or revoke remote credentials.

Deletion is blocked when a job, model or deployment revision references the name.
Registration and deletion serialize through a project row lock in PostgreSQL. The API's
explicit `force=true` permits an admin to remove a referenced secret, which can break
future starts and rollback; the UI uses protected deletion. Definitions remain immutable.

## Installation and recovery

Migration `0016` adds reference JSON to job definitions, models and deployment revisions;
existing rows default to no references. Upgrade the DB and install the updated API RBAC
before enabling this feature. A missing backend rejects secret operations/references.

Kubernetes Secret storage is not an external encrypted vault. Encryption at rest, access
to the Kubernetes API/etcd and backups are cluster responsibilities. Control-plane DB
backups include names and audit history **but no secret values**; restore project secrets
through your Kubernetes backup or original credential source before restarting workloads.

Local fake/API/manifest tests verify write-only responses, role isolation, references,
CAS requests, redacted errors and deletion protection. No cluster was started or changed.
Live gates remain: apply migration `0016`, verify actual namespace RBAC and ownership,
run a private-image job/function, rotate a credential and restart its consumer, and restore
the secret store alongside a control-plane DB backup.
