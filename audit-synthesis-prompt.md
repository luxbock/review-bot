You are merging whole-repository maintainability audits from {{N}} independent auditors of the
SAME repository into one ranked list.
Inputs: {{REVIEW_JSON_LIST}}.

- Deduplicate findings by (file, overlapping location, same root cause / same category).
- Where auditors AGREE on a finding, keep it and raise its confidence.
- Where they DISAGREE (one flags, others don't, or they contradict), keep it as a "question"
  labeled CONTESTED — do not assert it as fact.
- DROP findings raised by only one auditor at low confidence.
- Preserve the strongest, best-grounded rationale among the merged duplicates.
- Re-rank the merged findings most-severe-first (blocker → question, most-impactful first
  within a band) and keep the combined list within the ~15 hard cap.

Return the same JSON schema as a single audit (summary / findings; NO verdict), findings
most-severe-first. Note in "summary" where the auditors diverged.
Output ONLY the JSON object — no prose before or after, no markdown code fences.
