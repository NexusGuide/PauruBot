# AbanGateway DNS binding (stacked follow-up to PR97)

## Dependency and scope

This change is based on PR97 (`fix/mender-panel-dns-binding`, head
`e34dd22ebcd5c59181e3793857039b54dc0ef907`), not independently on main. Review/merge
PR97 first, then retarget this follow-up to main as appropriate; do not copy isolated
files without the shared transport module. No database migration is needed. No merge,
deployment, workflow, billing or branch-protection change is part of this PR.

AbanGateway has an operator-configurable HTTPS API prefix. Before this change,
`_safe_base` validated DNS, then the generic gateway HTTP client resolved the hostname
again for both create and verify. Baseline regression tests observed that the final
HTTP transport still received the hostname instead of the checked numeric address.
No live rebinding exploit, real bank API call or metadata request was performed.

## Implementation

- Extract PR97's validated-IP connector into `paurubot.security.http`, with a generic
  `UnsafeRequest` HTTPX error. The panel adapter preserves the existing PanelError
  contract. There is one connection implementation, not a copied payment variant.
- Add a stateless per-request client-selection hook to Gateway. Default vendor
  gateways still use their existing client factory; Aban alone selects the safe
  transport for its configured URL. No shop URL/key is stored on shared providers.
- Validate URL syntax/static ranges and reject credentials, query or fragment API
  prefixes before sending the bearer key. Retain valid nested API prefixes and the
  existing HTTPS requirement. Errors expose `gateway_error`, not addresses, keys or
  backend exception text in customer responses/logs.
- Resolve/validate the complete DNS answer in the transport and dial only those
  numeric addresses. Keep original Host/port, SNI and certificate hostname checks.
- Enforce public gateway addresses even when global private-panel approval is on.
  The payment gateway deliberately does not inherit panel private-network policy.
- Disable environment proxy resolution and automatic redirects for Aban. The
  returned customer checkout link is not server-followed and its semantics are not
  changed here. Sites requiring private gateways or ambient proxies need a separate
  explicitly reviewed design, not a silent exception to this guard.
- Only pre-HTTP connection failures may try another already-validated address.
  Read/write/protocol failures never replay an uncertain invoice-creation POST.
  Existing network/JSON errors remain PaymentError(`gateway_error`).

Amount units, authority/order matching, payment-state transitions, ledger posting,
webhook verification, callback URLs, mocks and other gateway protocols retain their
existing behavior. Explicit mock transports remain DNS-free test seams, while static
URL shape/range restrictions still apply.

## Validation

New regression cases cover create/verify numeric handoff, original Host/SNI/API path
and bearer header, metadata/private/mixed-answer denial, malformed/credential/query/
fragment URLs, error-log key redaction, DNS failure and DNS changes between phases,
no uncertain-body retries, approved-IP connection failover, redirect refusal,
concurrent shops on one provider without URL/key contamination, explicit mock
transports, default CA/hostname verification and ignored ambient proxy settings.

Real loopback TLS tests also exercise Aban's own client factory for a matching and
wrong DNS-name certificate. Only that fixture's address-policy result is replaced
with loopback for controlled routing; separate tests exercise the real public-only
policy. This is not evidence that production accepts private gateway addresses.
PR97's real TLS/pinning/driver suite remains in the regression set after extraction.
No live gateway, Telegram or staging transaction is implied.

## Not solved by this change

This is network destination binding, not financial exactly-once delivery. A provider
can still accept an external invoice before a process dies ahead of local commit;
durable pre-call intent and reconciliation remain necessary. Response-size caps,
strict wall-clock total-request deadlines, organization-specific egress firewall
policy and DNS/certificate rotation against a real gateway require separate work.
The shared helper does not automatically secure every other outbound integration.
