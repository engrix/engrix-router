# Security Policy

## Unofficial client

> Unofficial client. Not affiliated with any vendor. Subscription-backed providers may
> break at any time and may violate the vendor's terms of service; you use them at your
> own risk.

No account creation, no credential harvesting, no captcha/turnstile bypass, no detection
evasion. If something looks like that, it does not ship.

This is a boundary enforced by the repository layout, not by a disclaimer: providers whose
protocol is not public live in a separate private distribution (ADR-0002). Reverse-engineered
vendor material — keys, obfuscation alphabets, undocumented endpoint paths, impersonated
client-identity headers — never enters this repository, and a test enforces that.

## Reporting a problem

**The reporting channel is private.**

Contact: `<maintainer-contact>` — see the repository's listed contact.

- Open a **public issue only for a problem that is already public and not sensitive**:
  a crash, a wrong status code, a documentation error, a routing bug.
- Send anything **sensitive** to the private channel above. That includes credential or
  token handling problems, terms-of-service exposure, vendor-integration issues whose
  details reveal how an adapter talks to a vendor, and anything that would teach a vendor
  how to break the adapters. Do not open an issue for those and do not discuss them in
  comments.

There is no encryption requirement for the private channel; do not send secrets, only
enough detail to reproduce the problem.

## What this software holds

The gateway is a credential store as much as it is a router. Assume the following when you
decide where to run it:

- **Upstream tokens are stored in plaintext** in a local SQLite file, in the
  `connections.cred_json` column. There is no encryption at rest today. The file is the
  whole blast radius: whoever reads it holds every vendor account the gateway can spend.
  Protect it with filesystem permissions and keep it out of backups, images and containers
  that leave your machine. Conversation payloads can land in the same database when
  `observability.enabled` is true; switch it off if that is a problem for you.
- **The admin API is fail-closed.** Without `EROUTER_ADMIN_TOKEN` set in the environment,
  every `/api/*` route answers 503 — there is no default token, no first-run password, no
  recovery endpoint. With a token, `/api/*` can read connection metadata and write budgets,
  nodes and settings, so the token is the only thing between a network peer and your vendor
  accounts. It is compared to the environment value; treat it as a full-access secret.
- **Client keys are hashed.** The API keys clients use on `/v1/*` are stored as SHA-256
  hashes plus a short display prefix; the plaintext is returned once, at creation. This is
  not true of upstream vendor credentials (see the first bullet).
- **Proxy credentials never enter the database.** A proxy pool row holds only the *name* of
  an environment variable (`credential_hint`); userinfo in a stored `proxy_url` is rejected.
- **Header redaction** applies to every log line and every stored trace stage
  (`EROUTER_REDACT_HEADERS`).

## Network exposure

The default bind is `127.0.0.1:8450` and **it should stay that way on a workstation**.
A gateway on a shared network is a quota faucet with your credentials: `/v1/*` spends money
and `/api/*` manages the accounts that do. Loopback is not automatically trusted either —
client keys are required by default (`EROUTER_REQUIRE_CLIENT_KEY=true`), because any process
on the same machine would otherwise get a free, unauthenticated quota drain.

If you must bind `0.0.0.0` — which is what `deploy/Dockerfile` and `deploy/docker-compose.yml`
do — do it only behind a reverse proxy that terminates TLS and authenticates, or on a private
network you control, with `EROUTER_ADMIN_TOKEN` set and `EROUTER_REQUIRE_CLIENT_KEY=true`.

## Server-side request forgery

Node `base_url` values are typed by operators, so they are guarded before anything is saved
(`transport/url_guard.py`): loopback, link-local (cloud metadata, e.g. `169.254.169.254`),
private and reserved addresses, plus `localhost`/`.internal`/`.local` hostnames are rejected.
The escape hatch is the setting `nodes.allow_private_urls` (default `false`) and it is
deliberately explicit: only turn it on for a genuinely internal endpoint. Proxy URLs are
shape-checked and rejected outright if they carry credentials.

Redirects are not followed by the upstream HTTP client, so a guarded URL cannot bounce to an
unguarded one.

## What is NOT promised

There is **no support contract** for this project.

- It is maintained on a volunteer basis by one owner, in their own time.
- There is **no CVE process**, no CNA, no security advisory mailing list, and no embargo
  period. Report privately and you will get a reply when there is one.
- There is **no SLA**, no fix-time commitment, and no guaranteed compatibility with any
  vendor endpoint, including the ones that currently work.
- Apache-2.0 applies: this is provided **as is**, without warranty of any kind. Running it
  can spend your money and can get your vendor accounts limited or suspended.

If you need a supported gateway with a disclosure process and an uptime commitment, this is
not that product.
