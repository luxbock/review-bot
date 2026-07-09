You are merging {{N}} independent triages of the SAME issue into one.
Inputs: {{REVIEW_JSON_LIST}}.

- If the triagers AGREE on the disposition, keep it and raise confidence.
- If they DISAGREE, do not paper over it: pick the MORE CONSERVATIVE disposition (the one
  that commits least / asks for more evidence — `needs-info` beats a contested bucket, and
  `works-as-designed`/`wrong-repo` beat an unproven `genuine-bug`), lower the confidence,
  and note the divergence in the assessment.
- Keep the best-grounded citation among the inputs; drop any disposition whose grounding
  did not hold across triagers.
- Preserve the single most fruitful recommended action for the merged disposition.

Return the same JSON schema as a single triage (summary / assessment / grounding /
recommended_action / confidence / disposition). Note in "assessment" where the triagers
diverged. Output ONLY the JSON object — no prose before or after, no markdown code fences.
