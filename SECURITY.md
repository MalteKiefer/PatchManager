# Security policy

## Supported versions

The latest minor release of the current major version (1.x) receives security fixes.

## Reporting a vulnerability

Please do not open a public issue. Report privately through GitHub's
[private vulnerability reporting](https://github.com/MalteKiefer/PatchManager/security/advisories/new).

Include affected version, steps to reproduce, impact, and any proof of concept.

We aim to:

* acknowledge your report within 3 business days,
* give an initial assessment within 10 business days,
* release a fix for critical and high issues within 30 days, and
* credit you in the advisory unless you prefer otherwise.

Please give us reasonable time to fix the issue before public disclosure. We will not
take legal action against good faith research that respects this policy, avoids privacy
violations and service disruption, and only uses test data or your own installation.

## Scope

In scope: this repository's code, container images published under
`ghcr.io/maltekiefer/patchmanager`, and the shipped compose and proxy configuration.
Out of scope: third party services and vulnerabilities requiring a compromised host.

The threat model lives in [docs/security/threat-model.md](docs/security/threat-model.md).
