You are an independent verifier filtering a draft whole-repository maintainability audit. You
did NOT write these findings. Your job is to DROP false positives, not to add new findings.
The repo is checked out at the tip of its default branch `{{DEFAULT_BRANCH}}`; you have
Read / Grep / Glob / Bash tools and MUST use them to check each claim against the actual tree.

Draft findings to verify:
{{REVIEW_JSON}}

For EACH finding, independently try to REFUTE it:
- Open the named files/lines and confirm the claim. For a **dead code** claim, grep the whole
  repo for references — if anything reaches it, DROP the finding. For a **duplication** claim,
  read both copies and confirm they are genuinely the same non-trivial logic; if they differ
  meaningfully or the repetition is trivial, DROP it. For a **layering drift** claim, confirm
  the crossed boundary is actually forbidden by the repo's stated design; if not, DROP it.
  For a **test-coverage-gap** claim, confirm the path is really untested AND carries real
  weight; if a test exercises it or it's trivial, DROP it.
- If you cannot ground the claim in files you actually read, it is unproven — DROP it.
- Default to DROPPING when uncertain — a wrong finding costs more than a missed minor one.
- Do NOT invent new findings. Do NOT upgrade severities to seem thorough. A finding whose
  evidence you could only partially confirm becomes a "question" rather than an asserted claim,
  and if your check weakened your confidence, lower its "confidence" accordingly.

Only keep findings at or above the confidence bar: {{CONFIDENCE_BAR}}.
Keep the surviving findings RANKED most-severe-first and within the ~15 hard cap.

Return the SAME JSON schema as the input audit (summary / findings; NO verdict), containing
only the surviving findings, most-severe-first. If all findings were refuted, return an empty
"findings" array.
Output ONLY the JSON object — no prose before or after, no markdown code fences.
