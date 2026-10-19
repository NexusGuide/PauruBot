# Panel DNS validation is bound to the actual connection

## Finding and scope

S05: the original SafeHttpClient validated DNS but discarded the returned addresses,
then HTTPX resolved the original hostname again. The check was cached for 60 seconds.
Certificate inspection also checked DNS, then connected by hostname. An operator-
configured or compromised panel hostname with changing DNS could therefore evade
check/connect identity. This was a design gap, not a demonstrated live compromise.

The baseline regression tests reproduce both gaps by observing the URL delivered to
the real HTTP transport boundary and the arguments to certificate inspection's TCP
connector. No requests to real metadata endpoints or production services are made.

## Implementation

A dedicated HTTPX transport validates the complete DNS answer for each request and
passes only a validated numeric address to the underlying TCP/TLS transport. It does
not pass the logical hostname to the connector for a second lookup. Mixed public/
private answers are rejected before any connection unless the existing private-host
policy explicitly permits them; metadata/link-local/other always-blocked ranges
remain blocked even with private approval. IPv4-mapped IPv6 policy is preserved.

The logical request origin is kept separate:
- Host includes the original hostname and non-default port.
- `sni_hostname` carries the original ASCII/IDNA hostname, retaining normal CA and
  hostname verification (or the existing explicitly approved pinned-certificate mode).
- HTTPX's outer request/response and cookie jar retain the original origin, not the IP.
- Scheme, host, port and URL credentials cannot pivot to a different origin, even
  through an absolute request path. Host/SNI overrides are not accepted in production.
- Environment HTTP(S)/ALL_PROXY is deliberately ignored. A proxy that resolves the
  hostname itself would break this binding. Explicit safe proxy support is separate.

Validated IPs are attempted in resolver order for pre-HTTP ConnectError/ConnectTimeout
only. The shared request stream is never retried after read, write, protocol errors,
HTTP responses or uncertain side effects. DNS has a connect timeout; remaining
connect budget is reduced between IP attempts. HTTPX still applies separate TCP/TLS,
read/write/pool timeouts; this is not a strict wall-clock total-request deadline.

The 60-second validation cache is removed; pooled connections remain numerically
bound, but every new request still validates its current DNS answer. This costs an
additional DNS lookup per hostname request. Existing pool limits, circuit breaker,
no-redirect default, redaction and explicit private opt-in remain in effect. Injected
mock transports remain a trusted test seam and bypass real DNS as before.

Certificate inspection resolves and validates once, connects to those same numeric
addresses with the original server_hostname, tries the next address on OSError,
and closes/waits for the writer under one eight-second operation timeout. Inspection
still deliberately does not authenticate the fetched certificate: owner approval
of the fingerprint is required before trusting it, and no credentials are sent.

## Compatibility / rollout

No database migration. Restart API, bot and workers that own panel clients. HTTPX is
bounded to >=0.27.2,<0.29 (tested 0.27.2 and 0.28.1) in both dependency manifests; its HTTPcore
1.x SNI extension is part of this transport contract. Keep that compatibility test
when upgrading the dependency. No private attributes of HTTPX/HTTPcore are patched
in production. Egress firewall rules remain defense in depth; DNS binding does not
restrict what an already-approved panel can do internally or make HTTP encrypted.

This PR covers SafeHttpClient users (panel drivers and subscription fetches) and
certificate inspection. It does not claim to secure every outbound HTTP call.
In particular AbanGatewayProvider._safe_base separately validates a configurable
URL and then uses the generic payment HTTP client: its check/connect binding is
still a separate payment-provider follow-up. Fixed vendor endpoints and other
integrations are outside this panel transport change.

Existing networks relying on ambient proxy variables must be reviewed before rollout;
they are intentionally not silently honored. Live panel/provider networking, proxy
policy, DNS rotation and certificate rotation still require staging validation.

## Validation coverage

Regression coverage includes exact checked IP handoff, original Host/SNI and URI,
complete-answer rejection, DNS changes between requests, numeric IPv4/IPv6, safe
connection-only failover, no replay after uncertain HTTP errors, origin/header/SNI
pivot refusal, bounded DNS timeout and certificate-inspection address fallback.
A local real TLS server verifies DNS-name CA validation, wrong-name rejection,
original SNI/Host, login cookies across two connections and ignored environment proxy.
Existing self-signed pin acceptance/rejection tests and panel driver contracts remain
part of the focused suite. Local loopback uses explicit private approval; fake public
addresses are never contacted during tests.
