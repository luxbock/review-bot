You are merging code-review results from {{N}} independent reviewers of the same PR.
Inputs: {{REVIEW_JSON_LIST}}.
- Deduplicate findings by (file, overlapping line range, same root cause).
- Where reviewers AGREE on a finding, keep it and raise its confidence.
- Where they DISAGREE (one flags, others don't, or they contradict), keep it as a
  "question" labeled CONTESTED — do not assert it as fact.
- DROP findings raised by only one reviewer at low confidence.
- Preserve the strongest rationale/trace among the merged duplicates.
Return the same JSON schema as a single review. Note in "summary" where the reviewers
diverged.
Output ONLY the JSON object — no prose before or after, no markdown code fences.
