# Engineering Principles — Decision Authority & Code Quality

Canonical source: `ytgen-infra/principles.md`. This file is duplicated
verbatim into `.claude/rules/principles.md` in each of the five product
repos (YouTubeGen, youtubegen-saas-wrapper, youtubegen-web, tryon-bot,
youtubegen-motion-graphics-remotion — the fifth added 2026-09-06 when it
graduated from experimental spike to a real production repo under this
same framework). If you edit this content, edit the canonical copy in
`ytgen-infra` first, then propagate the same text to all five repos in
the same batch of commits — do not let copies drift.

## 1. Decision authority — when to act autonomously vs. stop and ask Danila

Apply this test, in order, whenever a question comes up mid-task:

1. **Is there an explicit principle in this document, or in this repo's
   own `CLAUDE.md`/`engineering.md`, that directly covers the situation?**
   Apply it yourself. Do not stop to confirm. Record what you did and why
   in the OPEN ITEMS section of your final report (see §3) so Danila can
   review it after the fact, not before.
2. **Is the situation irreversible or high-consequence** — real data loss,
   money, or a change to a contract/credential with an external service
   (payment provider, DNS, a third-party account) **that cannot be cheaply
   undone**? Stop. Ask.
3. **Is this a business-priority or scope tradeoff with no single correct
   technical answer** — e.g. ship now vs. wait, which of two valid designs
   to build first, whether a UX compromise is acceptable? Stop. Ask.
4. **Otherwise** — an engineering detail with no explicit principle
   covering it, and no step 2/3 condition — use your best judgment, note
   the decision and your reasoning in OPEN ITEMS, and keep working. Do not
   block the rest of the task waiting for an answer to something that
   isn't actually a blocker.

A missing external credential or account-level resource you have no way
to create yourself (e.g. a new healthchecks.io check, a new DNS record, a
new S3 bucket) is always a real blocker under step 2 — stop and ask,
regardless of which session type below you're in.

## 2. Session risk tiering — how much autonomy depends on what the session does

- **Read-only / diagnostic sessions** (reading logs, inspecting a dump,
  investigating why something failed, auditing current state) — work
  through to completion autonomously, without pausing for approval mid-
  session. Deliver one final report at the end. Nothing in this tier
  writes to production, so there's nothing to approve before it happens.
- **Any session that writes** — merges code, changes a live `.env`,
  creates or modifies an infrastructure resource, triggers a deploy — the
  existing gate applies in full: mandatory OPEN ITEMS block (§3) +
  mandatory `review-agent` pass (§4) before merge. This tier is
  unaffected by anything above; it is not made more autonomous by this
  document.

## 3. OPEN ITEMS — mandatory report format

Every final report to Danila, from every session, starts with an OPEN
ITEMS block — first thing in the report, before the summary of what was
done. If there is nothing open, say so explicitly ("OPEN ITEMS: none")
rather than omitting the section. This block includes:

- Every autonomous decision made under §1.1 or §1.4, with a one-line
  reason.
- Anything that needed a stop-and-ask under §1.2/§1.3, if the task
  continued in a degraded/partial form rather than blocking entirely.

`review-agent` Critical/Warning findings are excluded from this block
(changed 2026-09-22) — see §4 for what's still mandatory (the gate
itself running) versus what's no longer reported upward (its findings).
`frontend-qa-agent` findings in `youtubegen-web` are a separate gate and
still belong here, unaffected by this change.

Exclusion — routine mechanics are not decisions. Do not include routine
environment/setup actions that had no real alternative — starting a
required local service, installing a stated dependency, running a
documented test command — even if their effect (e.g. a local service left
running) persists after the session, unless that persistence carries a
concrete downside (cost, security exposure, conflict with another
process). These are execution detail, not a decision under §1.4. Include
an item here only if there was a genuine choice between valid
alternatives, an ambiguity you resolved, or a consequence with real risk
or cost.

## 4. `review-agent` is a mandatory gate, not a discretionary tool

Before any merge to `main`, in all five repos, `review-agent` must run.
This is not optional and not left to the implementing agent's judgment to
skip on a "simple" change. What to do about a Critical/Warning finding
(fix it, or consciously accept and proceed) is the implementing session's
own scope and responsibility — its findings are no longer part of the
OPEN ITEMS block and are not surfaced upward (changed 2026-09-22; this
previously required verbatim OPEN ITEMS reporting, same as
`frontend-qa-agent` still requires in `youtubegen-web` — that separate
gate's own reporting requirement is unchanged, this change is scoped to
`review-agent` only). This does not relax the gate itself: `review-agent`
must still actually run, via a real Task-tool call, before every merge —
see the self-certification rule immediately below, which is unchanged.

`youtubegen-web` currently only has `frontend-qa-agent` (visual/UX
review) — it is missing `implementation-agent`, `review-agent`, and
`test-agent`, which the other three repos already have. These three are
added to `youtubegen-web` as part of this same task, adapted from
`YouTubeGen`'s existing definitions in `.claude/agents/`. Do not merge
`frontend-qa-agent` and `review-agent`'s responsibilities — the former
stays scoped to visual/UX correctness, the latter to code/security/
convention review; they run as separate passes.

Applying the `review-agent` checklist manually, by the implementing
session itself, does not count as a passed gate — only a real call
through the Task tool counts. Background and parallel sessions may by
default lack access to the Task tool; if so, the session must explicitly
stop after committing and record, in its own report, "`review-agent` did
not run — Task tool unavailable," rather than claim the check passed
(this record stays in the session's own report, not the OPEN ITEMS block
— see §3's 2026-09-22 change above). Real incident: 2026-08-23, both
parallel sessions (saas-wrapper, tryon-bot) made exactly this
substitution simultaneously — caught and re-run manually from a session
with Task-tool access.

### 4.1 CI-level enforcement layer removed (2026-08-25) — session-level only, by design

A second, independent CI-level layer (`review-gate` job in
`.github/workflows/ci.yml`, calling `review-agent`'s logic via the
`claude` CLI authenticated with the CI review-auth credential) existed
briefly in all four repos and has been **removed**. Reason: two full
days (2026-08-24 to 2026-08-25) lost to the CI review-auth credential
operational pain — headless-session auth failures, and repeated
credential-exposure incidents (the token leaking into a session
transcript via a diagnostic command, a chat paste, a screenshot, and
twice via the `claude` CLI's own error-echo on rejection) — for a CI
job whose only job was re-running an LLM-based check that the
session-level gate below already performs. Danila made the explicit
call: accept the residual risk of no independently-CI-verified review,
in exchange for not carrying an LLM-in-CI dependency that had become a
recurring operational and security liability. `scripts/review_gate.py`
and `scripts/review_gate_prompt.md` are deleted from all four repos;
the CI review-auth credential is removed from all four repos' GitHub Actions
secrets and from the local shell profile that fed it. `deploy` now
depends directly on `test` in all four workflows.

**What this means going forward:** `review-agent` remains mandatory as
a session-level step — the main paragraph of §4 above is unchanged, and
the self-certification rule (checklist applied by the implementing
session itself doesn't count; a real Task-tool call does) still applies
in full. There is simply no second, CI-enforced check behind it anymore.
This is a conscious, accepted tradeoff, not an oversight — don't
"fix" it by re-adding a CI-level LLM check without this being raised
with Danila again first.

### 4.2 Verify per-repo state — never assume rollout uniformity

A rollout applied to "all four repos" is not verified until it is checked
independently in each of the four, using that repo's own tooling — not
inferred from "we already did this for all four earlier." Real incident:
the CI review-auth credential was believed present in all four repos
following an earlier rollout; direct per-repo `gh secret list` checks
later found it missing in three of the four (now moot — see §4.1, the
secret has since been removed from all four rather than completed).
This applies to any claim about cross-repo state — secrets, workflow
files, deployed config, not just this incident — verify independently in
each repo before relying on it or reporting it as done.

### 4.3 Credential hygiene

OAuth tokens and API keys are entered directly into a terminal
(`gh secret set`, a shell profile, a secrets manager) and never pasted
into a chat message or left sitting in a session transcript, including a
Claude Code session transcript. If a token is ever exposed this way for
any reason, treat it as compromised immediately and rotate it at
https://claude.ai/settings/claude-code — don't wait to confirm actual
misuse first. Real incident, 2026-08-25: the CI review-auth credential was
exposed at least four separate times in one session (a diagnostic `grep`
against a shell profile, a direct chat paste, a terminal screenshot, and
twice via the `claude` CLI's own error output echoing a rejected
`Authorization` header) — directly leading to §4.1's decision to remove
the credential and the CI layer that depended on it, rather than keep
chasing rotation.

## 5. Technical debt & code quality

- **YAGNI is the default everywhere.** Do not build abstractions, config
  options, or generalized interfaces for a need that hasn't been named.
  This is the existing, unchanged company principle — nothing below
  overrides it.
- **Open/Closed Principle applies only to the named extension points
  below** — never as a general coding style, and never speculatively.
  Code touching one of these points should be written so the named future
  change doesn't require rewriting it; code elsewhere should stay as
  simple and direct as the current requirement needs, even if it looks
  "less extensible."

  **Named extension points (v1):**
  - **LLM/model selection.** Currently hardcoded via env vars
    (`CREATIVE_PROVIDER`, etc.) through the full chain
    `youtubegen-web → youtubegen-saas-wrapper → YouTubeGen (core)`. This
    is expected to become a client-facing choice (the tenant picks a
    model/provider through the SaaS UI) at some future point. Code that
    reads or threads these provider/model values through the three
    services should not assume the value is a fixed, deploy-time
    constant — prefer reading it as a per-request/per-tenant parameter
    where that's cheap to do now, even while it's still sourced from env
    in practice today.

  This list is expected to grow. A new extension point is added here as
  its own explicit decision (discussed with Danila first) — never
  inferred or added unilaterally by an implementing session, no matter
  how likely the extension seems.

- **Backward compatibility.** Do not change or remove an existing
  contract between services (an HTTP endpoint's request/response shape,
  an env var another repo depends on, a shared secret's meaning) without
  an explicit migration plan: what changes, which repos need to deploy in
  which order, and how to detect anything left on the old contract. "Also
  update the other repo in the same PR" is not a migration plan on its
  own — the repos deploy independently (separate CI/CD pipelines,
  separate runners) and can be at different commits at any given moment
  in production.

- **No silently misleading client-facing state.** When the system writes
  non-client-authored content into a field a client can otherwise view or
  edit (e.g. an internal placeholder forced into a config value to satisfy
  a downstream schema constraint), that field must never be capable of
  displaying to a client as if it were their own real, ready-to-resubmit
  content. Either keep the field fully hidden while the placeholder is in
  effect, or have the UI start it visibly empty instead of pre-filling it
  from stored state whenever that stored value is known to be a system
  placeholder rather than something the client wrote. Real incident
  (2026-09-02, `YouTubeGen`/`youtubegen-web`, stock b-roll SaaS exposure):
  core auto-forces a placeholder string into `prompt_templates['video']`
  when a scene kind resolves to stock (necessary — a downstream Pydantic
  constraint requires non-empty content regardless of whether render time
  ever reads it); the web form correctly hid that field while stock was in
  effect, but a client switching a channel's workflow away from stock in
  the same edit session would see the raw placeholder text reappear as if
  it were real, editable content — one accidental save away from becoming
  that channel's actual production prompt. The failure mode (wrong render
  mode or wrong content shipping in a real video) is exactly the kind of
  highly visible, reputationally costly mistake this principle exists to
  prevent — treat it as worse than the extra friction of asking a client
  to explicitly (re)fill real content.

## 6. Incident response & failure-layer classification

This section applies only to a **real production incident** — actual
user-facing impact, data-integrity impact, or real cost/financial impact
that reached production undetected. A bug caught before it reached
production (in review, in CI, in testing) is not this — at most it's a
`Known technical landmines` entry (see each repo's `engineering.md`) or
a `Real incidents` entry (see each repo's `CLAUDE.md`), both unaffected
by this section. Do not apply this section's weight to routine bugs —
the goal is signal on real incidents, not a log of every mistake
(decided 2026-09-24).

### 6.1 Failure-layer classification (mandatory for every production incident)

Every production incident's writeup names which layer(s) its root cause
belongs to — this is not optional, and an incident can genuinely span
more than one:

- **Scoping failure** — the task spec (Danila → orchestrator → Repo
  Lead, or a direct session's own understanding of the ask) was too
  vague, or was silently altered mid-task instead of the change being
  surfaced (see `ytgen-infra/CLAUDE.md`'s "Escalation").
- **Implementation failure** — the spec was correct; the code was wrong.
- **Test-coverage gap** — the actual failure mode existed in the system
  but nothing in the test suite exercised it.
- **Review-gate gap** — `review-agent` either didn't run before the
  merge that shipped the bug, or ran and missed the finding. Note: §3's
  removal of routine `review-agent` OPEN ITEMS reporting does not apply
  here — if a real incident's root cause traces back to a missed or
  skipped `review-agent` pass, that fact belongs in the incident
  writeup. This is a narrow exception scoped to real incidents, not a
  reopening of routine reporting.
- **Observability gap** — the failure could have been caught or bounded
  earlier with better monitoring/alerting/logging, independent of
  whether the code itself was wrong.
- **Process/communication gap** — an Escalation-worthy conflict or
  decision existed and got resolved unilaterally instead of surfaced.

### 6.2 Incident writeup — required sections

What happened → root cause → failure-layer classification (§6.1) → why
it wasn't caught before production → the fix → independent verification
(not just the fixing session's own claim) → any resulting rule/process
change. A cross-repo or business-level incident is written up as a
standalone file in `ytgen-infra`'s root (the existing convention,
e.g. `stock-broll-script-gen-budget-incident-2026-09-21.md`); an
incident scoped to a single repo goes into that repo's own
`documentation.md`/`CLAUDE.md` "Real incidents" section instead of a
separate file.

Incident writeups are in English (changed 2026-09-24 — two earlier
`ytgen-infra` incident files were in Russian and have since been
translated).

## 7. Cross-reference

This file is about decision authority and code-quality philosophy across
all five repos. It does not replace or restate:
- Each repo's own `CLAUDE.md` (product-specific priorities and hard
  invariants) and `.claude/rules/engineering.md` (product-specific
  mechanics) — read those too; they're still authoritative for anything
  specific to that repo.
- `ytgen-infra` — hosts/domains/env-variable inventory and access
  mechanics; read that for anything involving production credentials,
  hosts, or the env-sync workflow.
