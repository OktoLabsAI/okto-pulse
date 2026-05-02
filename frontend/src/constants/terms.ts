/**
 * Terms-of-use constants for the acceptance modal.
 *
 * `TERMS_VERSION` follows the package release; `TERMS_HASH` is bumped any
 * time the legal text changes — a mismatch with the cached acceptance
 * forces a re-prompt. Keep TERMS_BODY a single Markdown string so the
 * modal can render it verbatim with `<MarkdownContent>`.
 *
 * Mantenha sincronizado com `LICENSE`, `TRADEMARKS.md` e `CLA.md` na
 * raiz do okto_labs_pulse_community. Quando esses arquivos mudarem,
 * copie as mudanças aqui e bumpe `TERMS_HASH` para forçar re-aceite.
 */

export const TERMS_VERSION = '0.1.13';
export const TERMS_HASH = 'tos-2026-04-29-elv2-addendum-trademark-cla-cr2026';

export const TERMS_BODY = `# Terms of Use & License — Okto Pulse

Welcome to **Okto Pulse**, distributed by **Okto Labs** under the
**Elastic License 2.0 (ELv2)** with the SaaS / Branding Addendum and
the additional trademark safeguards described below.

By clicking **"I have read, understood and accept"** at the end, you
acknowledge that you have read, understood, and agreed to **all** of
the terms in this document, including the LICENSE Addendum sections
I–IV, the Trademark Policy, and the Contributor License Agreement.

---

## 1. Elastic License 2.0 — Core Terms

### Acceptance

By using the software, you agree to all of the terms and conditions
below.

Copyright 2026 Okto Labs.

### 1.1 Grant of License

The licensor grants you a non-exclusive, royalty-free, worldwide,
non-sublicensable, non-transferable license to use, copy, distribute,
make available, and prepare derivative works of the software, in each
case subject to the limitations and conditions below.

### 1.2 Limitations

You **may not** provide the software to third parties as a hosted or
managed service, where the service provides users with access to any
substantial set of the features or functionality of the software.

You **may not** move, change, disable, or circumvent the license key
functionality in the software, and you **may not** remove or obscure
any functionality in the software that is protected by the license
key.

You **may not** alter, remove, or obscure any licensing, copyright, or
other notices of the licensor in the software.

### 1.3 Patent License

The licensor grants you a license, under any patent claims the
licensor can license, or becomes able to license, to make, have made,
use, sell, offer for sale, import and have imported the software.
However, this license does not cover any patent claims that you cause
to be infringed by modifications or additions to the software. If you
or your company make any written claim that the software infringes or
contributes to infringement of any patent, your patent license for
the software granted under these terms ends immediately. Your
company's patent license granted under these terms also ends
immediately if your company makes any written claim that the software
infringes or contributes to infringement of any patent.

### 1.4 Distribution

You may not alter, remove, or obscure any licensing, copyright, or
other notices of the licensor in the software. Any distribution of
the software must include a copy of these terms and conditions, and
anyone who receives the software from you is bound by these terms and
conditions.

### 1.5 Notices

You must include a copy of these terms with any distribution of the
software. If you modify the software, you must mark the modifications
clearly and include the date of the modifications.

### 1.6 Termination

If you violate these terms, your licenses will terminate
automatically. If the licensor notifies you of your violation, and
you cease all violation of this license no later than 30 days after
you receive that notice, your licenses will be reinstated
retroactively. However, if you violate these terms after the
reinstatement, all of your licenses will terminate permanently.

### 1.7 No Other Rights

Except as expressly stated herein, no other rights or licenses are
granted, express or implied.

### 1.8 Limitation on Liability

As far as the law allows, the software comes as is, without any
warranty or condition, and the licensor will not be liable to you for
any damages arising out of these terms or the use or nature of the
software, under any kind of legal claim.

### 1.9 Definitions

- **"Licensor"** means Okto Labs and its affiliates.
- **"Software"** means the software the licensor makes available
  under these terms, including any portions, modifications, or
  derivative works.
- **"You"** means you, individually.
- **"Your company"** means any legal entity, sole proprietorship, or
  other organization that you work for, plus all other organizations
  that control, are controlled by, or are under common control with
  that organization. The term "control" means ownership of
  substantially all the assets of an entity.
- **"Your licenses"** means the licenses granted to you in Section 1.1.
- **"Use"** means anything you do with the software in violation of
  these terms.

---

## 2. Addendum: SaaS, Competing Service, Internal Use, and Branding

This addendum clarifies and supplements Section 1.2 ("Limitations")
of the Elastic License 2.0. **In case of conflict between the body of
the license and this addendum, this addendum controls.**

### I. Hosted or Managed Service — PROHIBITED uses

For the purposes of Section 1.2, the following constitute providing
the software as a "hosted or managed service" and are **PROHIBITED**:

- **(a)** Operating a multi-tenant SaaS, platform, application, or
  API where end users from more than one client organization interact
  with the features or functionality of the software, whether
  directly or through a wrapper, proxy, or abstraction layer.
- **(b)** Providing the software as a white-label, embedded, OEM, or
  rebranded offering to third parties.
- **(c)** Offering the software, any derivative work of it, or any
  substantial portion of its features or functionality as a product,
  service, or platform that competes with Okto Pulse, regardless of
  whether the deployment is single-tenant or multi-tenant.
- **(d)** **Internal large-scale platform exposure** — operating the
  software as an internally hosted service that meets BOTH of the
  following conditions simultaneously:
    - **(i)** the software, or modules extracted, externalized, or
      repackaged from it, are exposed as a hosted service, API, or
      internal platform to users within your organization who are
      not directly involved in administering, developing,
      configuring, or using the software for the projects, teams, or
      products it is intended to manage; **AND**
    - **(ii)** the total population of such exposed users exceeds
      five hundred (500) individuals.

  Both conditions (i) and (ii) must be met for this prohibition to
  apply. Local, desktop, or workstation use of the software by any
  number of individuals is **never** restricted by this clause.

  This clause exists to prevent large organizations from extracting
  modules of the software and serving them at scale as an internal
  platform-as-a-service to their broader workforce. It is **not**
  intended to limit normal collaborative use by project teams, no
  matter how large the organization.

### II. PERMITTED uses

The following are expressly permitted, including for **commercial**
purposes and including when financial consideration is exchanged:

- **(a)** Using the software internally within your organization,
  without any numeric or scale restriction, to manage your own
  projects, teams, products, or operations.
- **(b)** Hosting a single-tenant deployment of the software for
  yourself or for a single client organization. A single-tenant
  deployment may serve multiple projects, teams, departments, or
  business units of the same client organization within the same
  instance ("classic single-tenant"). Each distinct client
  organization must receive its own dedicated instance.
- **(c)** Providing consulting, integration, customization,
  deployment, or managed operations services using the software,
  including charging fees for such services, **provided that**:
    - **(i)** each client deployment is single-tenant as defined in
      (b);
    - **(ii)** the branding and attribution requirements in
      Section III are fully honored in every deployment;
    - **(iii)** the offering does not constitute a competing service
      under Section I(c) and does not exceed the thresholds of
      Section I(d).
- **(d)** Integrating the software's MCP tools with AI agents for
  your own use or your organization's use.
- **(e)** Modifying the software for personal or internal
  organizational use, including for commercial purposes, subject to
  Sections I and III.

### III. Branding and Attribution — REQUIRED preservation

You **may not** alter, remove, obscure, hide, replace, minimize, or
otherwise diminish the visibility of any of the following in any
distribution, deployment, or derivative work of the software:

- **(a)** The "Okto Labs" name and logo.
- **(b)** The "Okto Pulse" name and logo.
- **(c)** Copyright and licensing notices identifying Okto Labs as
  the licensor.

These elements **MUST** remain visible to end users in:

- **(a) The web user interface**, including but not limited to:
  login and authentication screens; primary navigation and
  application chrome; the application footer; settings, admin, and
  operations consoles; any "About", "Help", or "Powered by" surface.
- **(b) The command-line interface (CLI)**, including but not
  limited to: the output of \`--version\` and equivalent commands;
  help and usage screens; any startup banner, splash, or login
  output.

You **MAY** add your own branding, logos, or attributions alongside
the required Okto Labs and Okto Pulse marks, but you **may NOT**
replace, substitute, or visually subordinate them in a way that
misrepresents the origin of the software.

### IV. Clarifications

If you are unsure whether your intended use constitutes a competing
service under Section I(c), an internal large-scale platform
exposure under Section I(d), or otherwise requires a commercial
license, contact the licensor at **dev@oktolabs.ai** for
clarification or to negotiate a commercial agreement.

---

## 3. Trademark Policy

This policy governs the use of Okto Labs and Okto Pulse names,
logos, and related marks. It complements (and never overrides) the
attribution obligations set forth in Section 2.III above.

### 3.1 Trademarks

The following are trademarks of **Okto Labs**:

- **Okto Labs** — the company name and brand
- **Okto Pulse** — the product name
- **Okto Labs logo** — the octopus/circuit design mark (all variants)
- **Okto Pulse logo** — the product logo and all variants

### 3.2 Relationship to the LICENSE

Section 2.III of the LICENSE ("Branding and Attribution") **requires**
that the Okto Labs and Okto Pulse names and logos remain visible in
the web UI and CLI of every distribution, deployment, and derivative
work of the software, as a notice of origin and attribution.

Nothing in this trademark policy waives, narrows, or overrides those
attribution obligations. **In case of apparent conflict between this
policy and Section 2.III of the LICENSE, the attribution obligations
of the LICENSE prevail.**

This policy governs the **use of the marks as identifiers of your
own product, service, company, or domain** — which is a separate
matter from attribution.

### 3.3 Permitted Use

You **may**:

- Preserve and display the Okto Labs and Okto Pulse names and logos
  as required by Section 2.III of the LICENSE (this is mandatory,
  not optional).
- State that your project, fork, or derivative is "based on Okto
  Pulse", "powered by Okto Pulse", or "compatible with Okto Pulse".
- Describe consulting, integration, deployment, or managed-operations
  services using factual references such as "We host Okto Pulse for
  our clients", "Okto Pulse consulting and integration", or "Managed
  Okto Pulse deployment operated by [Your Company]".
- Use the Okto Pulse name in factual references in documentation,
  articles, comparisons, conference talks, and academic work.
- Use the names when required by the LICENSE attribution notice.

### 3.4 Prohibited Use

You **may not** without prior written authorization:

- Use "Okto Pulse", "Okto Labs", "Okto", or any of the logos as part
  of **your own** product name, service name, company name, or domain
  name (e.g. \`oktopulse.example.com\`, "AcmePulse", "PulseHub",
  "Okto-Plus").
- Create the impression that your product, service, company, or fork
  is endorsed, sponsored, certified, or affiliated with Okto Labs.
- Use any of the logos, or a confusingly similar mark, as the
  **primary brand** of your product, marketing materials, app store
  listings, or social presence.
- Offer a product or service under a name that combines "Okto" with
  "Pulse", "Labs", or similar terms.
- Modify, distort, recolor, or recompose the logos in a way that
  misrepresents the marks.

### 3.5 Derivative Works and Forks

If you create a fork or derivative work:

- You **must add** your own distinct name, logo, and brand identity
  to identify the fork or derivative — and that name **must not**
  include "Okto", "Pulse", or any confusingly similar term (see
  Prohibited Use).
- You **must retain** the Okto Labs and Okto Pulse names and logos
  in the web UI and CLI as an attribution-of-origin, exactly as
  required by Section 2.III of the LICENSE. The required form is
  roughly: **"Powered by Okto Pulse — © Okto Labs"**, together with
  the official logos.
- You **must retain** the copyright and license notices required by
  the LICENSE.
- You **may** describe your work factually as "based on Okto Pulse"
  or "a fork of Okto Pulse".

In short: **rename your fork, but keep the attribution.**

### 3.6 Logo Usage

The official logo and brand assets in the source repository —
including the Okto Labs and Okto Pulse logos, wordmarks, icons, and
favicon under \`frontend/src/assets/\` (and any successors or
variants under the same paths) — are provided for two purposes:

1. **Mandatory attribution use** under Section 2.III of the LICENSE
   in the web UI and CLI of distributions, deployments, and
   derivative works. This use is required and pre-authorized.
2. **Factual reference use** in documentation, articles, comparisons,
   and presentations about the software.

The same assets **may not** be used as the primary brand of any
third-party product, service, fork, marketing campaign, merchandise,
or domain. They are not licensed for re-branding, white-labeling,
or unrelated commercial promotion.

**Canonical asset locations** (under \`frontend/src/assets/\`):
\`logo-light.png\`, \`logo-dark.png\`, \`oktolabs-icon.svg\`,
\`pulse-icon.svg\`, \`pulse-wordmark.svg\`,
\`pulse-wordmark-light.svg\`, \`favicon.jpg\`.

---

## 4. Telemetry & Data Privacy

The community edition is **local-first**:

- **No telemetry** is sent without explicit consent. Error logs and
  internal metrics stay only on your disk (\`~/.okto-pulse/\`).
- All data (boards, ideations, refinements, specs, sprints, cards,
  KEs, mockups, comments, Q&A) is stored **locally** in SQLite and
  in files under \`~/.okto-pulse/\`.
- You are responsible for backups, encryption at rest, and access
  control on your host.

---

## 5. Contributions — Contributor License Agreement (CLA)

By submitting a contribution (code, documentation, or other
materials) to this project, you agree to the terms below. The
authoritative text lives in \`CLA.md\` in the source repository.

### 5.1 Definitions

- **"You"** means the individual or legal entity submitting a
  contribution.
- **"Contribution"** means any original work of authorship submitted
  to the project, including modifications or additions to existing
  work.
- **"Project"** means the Okto Pulse software and related
  repositories maintained by Okto Labs.

### 5.2 Grant of Rights

You grant to Okto Labs a perpetual, worldwide, non-exclusive,
no-charge, royalty-free, irrevocable license to:

- Use, reproduce, modify, display, perform, sublicense, and
  distribute your Contribution as part of the Project.
- Relicense your Contribution under any license, including
  proprietary licenses.

### 5.3 Ownership

You retain copyright ownership of your Contribution. This agreement
does not transfer ownership — it grants a license.

### 5.4 Representations

You represent that you are legally entitled to grant the above
license; that your Contribution is your original work (or you have
the right to submit it); that your Contribution does not violate any
third party's rights; and that, if your employer has rights to
intellectual property that you create, you have received permission
to make the Contribution on behalf of that employer or your employer
has waived such rights.

### 5.5 No Obligation

Okto Labs is not obligated to use your Contribution. Contributions
may be accepted, modified, or declined at the project maintainers'
discretion.

### 5.6 How to Sign

By submitting a pull request to any Okto Pulse repository, you
indicate your agreement with this CLA. No separate signature is
required. If you are contributing on behalf of a company, please
ensure an authorized representative agrees to these terms.

---

## 6. Acceptance

By clicking **"I have read, understood and accept"** below, you
confirm that:

1. You are at least 18 years old or have equivalent legal capacity.
2. You have read all of the terms above, including the LICENSE, the
   Addendum (Sections I–IV), the Trademark Policy, the data privacy
   notice, and the CLA.
3. You agree to the **Elastic License 2.0** with the SaaS / Branding
   Addendum (Sections I–IV) and to the Trademark Policy.
4. You understand that destructive actions (delete board,
   archive_tree, restore_tree) are irreversible without prior backup.
5. You will **not** use the software to operate a multi-tenant SaaS,
   competing service, internal large-scale platform exposure exceeding
   500 unrelated users, white-label, or any other prohibited use
   listed in Section 2.I.
6. If you fork or derive the software, you will rename it (without
   "Okto"/"Pulse") and **retain** the Okto Labs and Okto Pulse names
   and logos as attribution in the web UI and CLI as required by
   Section 2.III of the LICENSE.
7. You acknowledge that contributions submitted to the project are
   governed by the CLA in Section 5.

The full canonical texts live at \`LICENSE\`, \`TRADEMARKS.md\`, and
\`CLA.md\` in the source repository. For legal questions or to
negotiate a commercial license, contact **dev@oktolabs.ai** before
proceeding.

Copyright 2026 Okto Labs. All rights reserved.
`;
