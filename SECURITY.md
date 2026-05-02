# Security Policy

## Supported Versions

Only the most recent minor release line receives security fixes. Older
versions are end-of-life and will not receive patches.

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x:                |

## Reporting a Vulnerability

If you believe you have found a security vulnerability in `okto-pulse`,
please report it privately so we can work on a fix before it becomes
public. **Please do not open a public GitHub issue for vulnerability
reports.**

### How to report

Use GitHub's **private vulnerability reporting** feature on this
repository: https://github.com/OktoLabsAI/okto-pulse/security/advisories/new

If you cannot use GitHub's reporting flow, email
**security@oktolabs.ai** with:

- a description of the vulnerability and its impact,
- steps to reproduce (proof-of-concept welcome),
- any version, configuration, or environment details that affect the
  attack surface,
- whether you intend to publish your own write-up, and on what timeline.

### What to expect

- **Acknowledgement** within **2 business days** of receipt.
- **Initial triage and severity assessment** within **5 business days**.
- **Fix timeline** depends on severity:
  - Critical: target patch within 7 days.
  - High: target patch within 14 days.
  - Medium / Low: rolled into the next normal release.
- We aim to coordinate disclosure with the reporter. Public disclosure
  happens after a fix ships, unless an in-the-wild exploit forces an
  earlier announcement.

### Scope

In scope:
- Code in this repository (CLI, frontend bundle).
- Code in the companion engine repo,
  [okto-pulse-core](https://github.com/OktoLabsAI/okto-pulse-core).
- The published Docker image
  `ghcr.io/oktolabsai/okto-pulse`.
- Default configuration shipped with the above.

Out of scope:
- Third-party dependencies (please report upstream first; if you
  believe our usage compounds the issue, include that in the report).
- User-deployed instances we do not operate.
- Social-engineering attacks against project maintainers.

## Coordinated Disclosure

By submitting a report you agree to give us a reasonable window to
patch before any public discussion. We will credit reporters who wish
to be credited once a fix has shipped.

## Related Documents

- [Contributing Guide](./CONTRIBUTING.md) — general contribution flow.
- [CLA](./CLA.md) — required for accepted code contributions.
