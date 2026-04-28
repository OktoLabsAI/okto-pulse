/**
 * Terms-of-use constants for the acceptance modal.
 *
 * `TERMS_VERSION` follows the package release; `TERMS_HASH` is bumped any
 * time the legal text changes — a mismatch with the cached acceptance
 * forces a re-prompt. Keep TERMS_BODY a single Markdown string so the
 * modal can render it verbatim with `<MarkdownContent>`.
 */

export const TERMS_VERSION = '0.1.6';
export const TERMS_HASH = 'tos-2026-04-28-elastic2-trademark';

export const TERMS_BODY = `# Terms of Use & License — Okto Pulse

Welcome to **Okto Pulse**, distributed by **Okto Labs** under the
**Elastic License 2.0 (ELv2)** with additional trademark safeguards
described in \`TRADEMARKS.md\`.

---

## 1. Elastic License 2.0 (summary)

You may:
- use, copy, distribute, modify and create derivative works of the
  software for **internal** purposes;
- redistribute copies of the software (modified or not) under this same
  license, preserving all notices.

You may **not**:
- offer the software as a **managed, hosted or SaaS service** to third
  parties;
- remove, obfuscate or circumvent license mechanisms, legitimate
  telemetry or copyright notices;
- use Okto Labs trademarks, logos or visual identity without explicit
  written permission (see \`TRADEMARKS.md\`).

Accepting these terms does **not** transfer intellectual property to
Okto Labs over anything you build with the software (your work remains
yours).

---

## 2. Trademarks

\`Okto Pulse\`, \`Okto Labs\`, the Okto logo and related visual
identities are trademarks — registered or in registration — of Okto
Labs.

**Community** forks must be renamed (without the term "Okto") when
publishing binaries, distributing extensions or making them publicly
available. Details in \`TRADEMARKS.md\`.

---

## 3. No warranty

The software is provided **"as is"**, without warranties of any kind,
express or implied, including but not limited to warranties of
merchantability, fitness for a particular purpose and
non-infringement. In no event shall Okto Labs be liable for any direct,
indirect, incidental, special or consequential damages arising from
the use or inability to use the software.

---

## 4. Telemetry

The community edition is **local-first** and **does not send telemetry**
without explicit consent. Error logs and internal metrics stay only on
your disk (\`~/.okto-pulse/\`).

---

## 5. Data privacy

All data (boards, ideations, refinements, specs, sprints, cards, KEs,
mockups, comments, Q&A) is stored **locally** in SQLite and in files
under \`~/.okto-pulse/\`. You are responsible for backups, encryption
at rest and access control on your host.

---

## 6. Acceptance

By clicking **"I have read, understood and accept"** below, you confirm:

1. you are at least 18 years old or have equivalent legal capacity;
2. you have read the terms above;
3. you agree to the Elastic License 2.0 and to the trademark policy;
4. you understand that destructive actions (delete board, archive_tree,
   restore_tree) are irreversible without prior backup;
5. you will not use the software to create managed services or SaaS
   offerings for third parties without a separate commercial license
   from Okto Labs.

The full license text is in \`LICENSE\`. Full trademark policy in
\`TRADEMARKS.md\`. For legal questions, consult a lawyer before
proceeding.
`;
