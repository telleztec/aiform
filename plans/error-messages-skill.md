# Plan: error-messages skill

Approved by the repo owner on 2026-10-01 through the decision page
https://claude.ai/artifact/DFp4RGyCcSgPNMWiyiB46u (plan id
`error-messages-skill`, page version `2026-10-01T18:09:12Z`), then confirmed in
chat. Recorded choices: `d1` keep-error, `d2` existing-log, `d3`
skill-and-review, `d4` new-only, `approve` approved.

Note from the owner: open a P3 issue to review every existing message against
this standard. It depends on this work merging.

Pairing: Sonnet 5 authors, Opus 5 reviews.

## What is built

- `.claude/skills/error-messages/SKILL.md`, one rule per bullet, no history.
- One line in `PROCESS.md`'s review step: new or changed error strings pass the
  error-messages skill.
- No code, no change to any existing message, no new test.

## The four rules

1. Say what happened: one short sentence naming the problem and the offending
   value, flag or resource, quoted.
2. Say what to do: a short instruction after the symptom, as the exact flag or
   command where one exists. If the user can do nothing, it is a bug: say so and
   point at the log.
3. Non-actionable detail goes to the log (`.aiform/logs/`), not to the user.
   No stack traces, internals or secrets in user text.
4. Write like a person: plain statement, no LLM habits.

Additions from research: plain words and no blame, no "illegal" (NN/g, Rust);
lowercase start and no trailing period for one-liners (GNU, Rust); do not treat
stderr as a log (clig.dev); optional short error id shared by stderr and log
(OWASP).

## Decisions

- Prefix stays `Error:`.
- The existing `.aiform/logs/` is the home for non-actionable detail; no audit
  file.
- Enforcement is the skill and the review checklist; no banned-phrase test.
- Applies to new and changed messages only; the sweep of existing messages is a
  separate P3 issue.

## Skill contents

- Format and the four rules.
- Checklist for author and reviewer: names the value; says what to do; one
  sentence plus at most one action line; no internals or secrets on stderr;
  right exit code; detail in the log; no banned phrase.
- Banned phrases and replacements: "Oops!" and "Something went wrong" (state the
  symptom); "It seems that" and "appears to" (say it flat or say what was
  checked); "Unfortunately", "I apologize", "Successfully", "simply", "just"
  (delete); "please" before an instruction (drop it); "Failed to X" (`cannot X:
  reason`); "Invalid X" (`X 'val' is not allowed: rule`); "!" and dash
  flourishes (full stop or colon).
- CLI mapping: stderr, exit 2 for usage and operational errors, exit 1 for a
  verdict (declined gate, unhealthy).
- API shape after RFC 9457: `type` (stable code), `title`, `detail`, `status`,
  `instance`; extensions `param` or `errors[]`, `request_id`, `doc_url`;
  messages are English text separate from the stable code; traceback, params
  and upstream bodies go to the log.
- Three or four before/after examples from the repo, marked as examples and
  not applied.

## Sources opened

clig.dev, RFC 9457, Google AIP-193, NN/g error guidelines, Python argparse,
GNU Coding Standards, OWASP error handling, Rust compiler guide, Stripe errors,
GitHub CLI exit codes, Google and Microsoft tone guides. Read through a small
model's summaries, so treat quotes as paraphrase. Not verified: Microsoft error
pages (404), Elm, Apple, POSIX, sysexits.
