# Panel DNS binding (S05)

## Scope and behavior

`SafeHttpClient` now supplies a guarded HTTPX transport. Before every real request it
validates every address in the destination DNS answer, and sends the inner transport
request to a numeric address from that same answer. It does not resolve the panel
hostname again when connecting. Host and TLS SNI remain the original authority, as
does the outer HTTPX request/response identity used for cookies. A client is restricted
to its configured scheme/host/port; another origin needs its own client. This also
prevents sharing an IP-keyed connection pool between different HTTP/TLS identities.

DNS changes to a forbidden address fail closed on subsequent requests, even if there
is a cached connection. Mixed allowed/blocked answers fail as a whole. Existing
private-address opt-in and always-blocked metadata/link-local ranges are unchanged.
This is the existing private opt-in, not a new per-IP allowlist.

Connection failures can try another address from the validated answer. Read/write,
protocol and HTTP errors never trigger this fallback: an uncertain provider mutation
must be reconciled, not blindly replayed. Resolution is bounded by the connect timeout;
remaining connect budget is reduced between attempts. This is not a new end-to-end
request deadline (HTTPX still has phase-specific network timeouts).

The certificate inspection helper also connects to validated numeric addresses,
keeping original SNI and trying the other validated addresses on connection failure.
Its existing TOFU semantics are unchanged: inspection is not approval or authentication.
No claim is made here about changing the existing certificate trust model.

## Deployment and compatibility

- No database migration, API rename, financial policy or dependency change.
- `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` and environment proxy mounts are deliberately
  ignored for these panel clients. A proxy would resolve/connect on our behalf and
  invalidate the IP binding guarantee. Review deployments that currently require a proxy;
  do not silently restore `trust_env=True` as a workaround. Use an explicitly reviewed
  egress design before deploying there.
- Requests to a different scheme/host/port are now refused before credentials are sent.
  Callers fetching another origin must construct another `SafeHttpClient`, as the
  external-subscription adapter already does. Review all provider-specific callers.
- Redirect following remains off, even if a caller passes `follow_redirects=True`.
- Explicitly injected test transports retain the existing offline bypass contract;
  they are trusted test hooks, not a safe extension point for untrusted production input.
- Keep network-level egress blocks and timeout/rate limits. This change does not protect
  against a malicious host OS, routing/NAT changes or every outbound network path.

## Validation record

Source: main `d1a4c1d0ee2499bd226de12a032e73c84044016d`, independent of PRs #89–96.
The new file has 28 test cases; 13 fail and 15 pass on the unchanged baseline (no fixture
errors). On the patched source, all 28 new cases plus the 4 existing TLS-pinning cases
pass. Counts across environments are repeated runs, not additional unique tests.

The tests cover IPv4/IPv6 destination binding, original Host/SNI, actual loopback TLS
with normal hostname verification, original cookie domain, metadata and mixed answers,
DNS changes, private opt-in, timeout, connect-only fallback, uncertain-delivery no-retry,
proxy bypass prevention, cross-origin refusal, redirect behavior and certificate probes.

Local validation uses an isolated extraction of the exact config/base/SSRF/redaction/HTTP
modules, with minimal package initializers; it is **not a full repository checkout**.
The repository's root conftest and its autouse Redis cleanup fixture were not loaded.
No product function was replaced to make assertions pass; DNS/wire mocks are explicit
inside the tests, and real TLS runs only on loopback with an ephemeral test certificate.
The test blocks non-loopback socket destinations in that real-TLS scenario.

Validated with Python 3.12.15 and 3.13.14, HTTPX 0.28.1/HTTPCore 1.0.9; the same 32 cases
also pass with minimum declared HTTPX 0.27.0 on Python 3.13.14. Targeted Ruff lint and
formatter pass for the changed Python files; targeted mypy passes for `panels/http.py`;
compile succeeds. These are not whole-project lint/typecheck claims.

Full backend/frontend suites, integration with all panel drivers and the normal
repository test fixtures, combined testing with the other open fixes, GitHub Actions,
staging and live-provider QA have **not** been validated for this patch. The PR remains
a draft pending those checks. No merge, deploy, billing change or CI weakening occurred.
