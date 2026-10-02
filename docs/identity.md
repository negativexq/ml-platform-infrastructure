# Identity: sign-in and project roles

Every API request is made by someone, every change in the audit trail names them, and what
they may do depends on their role in the project.

## Model

| Role | May |
| --- | --- |
| **invoker** | only call the project's models: through the gateway (public endpoints) or the platform's predict route. Reads nothing. For services and partners that sign in with their own credentials (OAuth client credentials); a plain API key is the other way in (`docs/gateway.md`) |
| **viewer** | + read everything in the project |
| **operator** | + start, cancel and retry runs; register, evaluate and promote models; create deployments, deploy, start and abort canaries, roll back |
| **admin** | + delete the project, change acceptance thresholds, decide who is a member, open endpoints to the outside, issue and revoke API keys |

* A **member** is a user (`user:alice`) or a group from the identity provider (`group:ml-team`).
  A person's role is the highest of their own and their groups'.
* Whoever **creates a project** becomes its first admin. A project always keeps at least one
  admin: demoting or removing the last one is refused.
* **Platform admins** (`CP_PLATFORM_ADMINS`, e.g. `group:platform-admins`) may do anything in
  every project. Anyone signed in may create a project and sees only the projects they belong to.
* **GPU quotas are platform decisions.** GPUs are shared by the whole platform, so only
  platform admins set how many a project may hold (`PUT /projects/{project}/gpu-quota`, the
  `platform-admin` rule in the policy table). A project's own admins see the quota and its
  use but cannot raise it. The platform also refuses a quota below what is in use.
* The policy is a table, `controlplane/api/auth.py: POLICY`, read as: GET needs viewer, any other
  method operator, unless the table says otherwise. It fails closed: a route that names no
  project and is not in the table is refused, and `test_every_route_has_an_owner_in_the_policy`
  fails the build until a new route is placed deliberately.
* Memberships are rows in PostgreSQL (`memberships`, migration 0009), changed through the API
  or the UI (project **Settings → Members**), audited like everything else
  (`membership.granted / changed / revoked`).

No OPA: roles are four ranks checked in the application. Policy rules that span systems
(e.g. "prod deploys need a canary", shared with admission control) are where OPA would earn
its place; none exist yet.

## Authentication

Any OpenID Connect provider: the control plane only knows an issuer URL.

* **Scripts and CI:** `Authorization: Bearer <access token>`. The token's signature (the
  issuer's JWKS, cached, refetched on key rotation), issuer, expiry and audience
  (`CP_OIDC_AUDIENCE`, default `mlp`) are checked.
* **Browser:** the authorization-code flow with PKCE runs **on the server** (`/auth/login`,
  `/auth/callback`). The browser only ever holds an HttpOnly, SameSite=Lax session cookie,
  signed with `CP_SESSION_SECRET`; no token reaches the page, and the UI's CSP
  (`connect-src 'self'`) is unchanged. A browser request that changes something must also send
  `x-mlp-csrf: 1`, which another site cannot make a browser send. Sign-out ends the provider's
  session too (`end_session_endpoint` with `id_token_hint`).
* The username comes from `CP_OIDC_USERNAME_CLAIM` (default `preferred_username`, then
  `email`, then `sub`), groups from `CP_OIDC_GROUPS_CLAIM` (default `groups`; Keycloak group
  paths like `/ml-team` become `ml-team`).
* Role changes made in the platform apply at the next request. Group changes made in the
  identity provider apply at the next sign-in (the session carries the groups; 8 h by default).

## Configuration

| Setting | |
| --- | --- |
| `CP_AUTH_MODE` | `oidc` (default) or `none`. The API refuses to start in `oidc` mode without an issuer; `none` makes everyone an anonymous platform admin and logs a warning. `make cp-run` and `make cp-demo` default to `none` |
| `CP_OIDC_ISSUER` | e.g. `https://keycloak.example.com/realms/mlp` |
| `CP_OIDC_AUDIENCE` | required `aud` of API access tokens (default `mlp`) |
| `CP_OIDC_CLIENT_ID`, `CP_OIDC_CLIENT_SECRET` | the confidential client for browser sign-in; leave empty for API tokens only |
| `CP_SESSION_SECRET` | 32+ random characters; rotating it signs everyone out |
| `CP_PUBLIC_URL` | where users reach the platform, for the redirect URI (`https://mlp.example.com`) |
| `CP_PLATFORM_ADMINS` | comma-separated subjects |
| `CP_SESSION_HOURS` | default 8 |

## Locally, with Keycloak

`k8s/identity/` has a dev-mode Keycloak and a realm (`realm-mlp.json`) with three users
(password = username): **alice** (group `platform-admins`), **bob** (group `ml-team`) and
**carol**, a confidential client `mlp-ui` for the UI and a public client `mlp-cli` for
scripts (password grant, local only), with a `groups` claim and the `mlp` audience.

```bash
make identity-up                                     # prints the rest
kubectl -n identity port-forward svc/keycloak 8180:8080
CP_AUTH_MODE=oidc CP_OIDC_ISSUER=http://localhost:8180/realms/mlp ... make cp-demo
python scripts/identity_e2e.py                       # browser + API, end to end
```

With sign-in on, the demo gives `group:ml-team` operator on `credit-risk` and `carol` viewer
on `fraud-detection`, so the three users see three different platforms.

## Verified

* In CI, against an in-process OpenID Connect provider with real RS256 keys
  (`controlplane/tests/fake_idp.py`): token validation (wrong key, expired, wrong audience or
  issuer, garbage), every role boundary, groups, platform admins, the last-admin rule, the
  audit actor, the browser flow (state, nonce, PKCE, CSRF header, forged cookie, open-redirect
  attempts) and, in a real browser, sign-in, viewer/admin UIs, member management and sign-out.
* **Against a real Keycloak 26.4** (`scripts/identity_e2e.py`): sign-in through Keycloak's
  login page for all three users with the expected roles, sign-out through Keycloak's
  end-session endpoint, and API calls with Keycloak-issued bearer tokens.

Not yet: the control plane deployed in the cluster (its manifests do not exist yet; the issuer
must then be one URL that both the browser and the pod resolve, i.e. an ingress host), a
production Keycloak (database, TLS, HA), and service accounts for CI (client-credentials
tokens work as bearer tokens once the client has the `mlp` audience and a membership).
