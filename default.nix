# review-bot — the reusable PR-review routine (subtask #3 core).
#
# A stdlib-only Python program that runs the portable review prompt on a
# selectable engine (claude / codex), then posts ONE Markdown comment to a
# Forgejo PR as the read-only review-bot identity (REST + token, never `fj`).
# See pkgs/review-bot/review.py and notes/decisions/forgejo-multi-identity.md.
#
# The harness binaries (`claude` / `codex`) are resolved from PATH at RUNTIME,
# not baked in: `claude` must be the agent's wrapped binary (plugins/settings),
# and codex is added to the agent's PATH separately. `git` IS baked in so the
# routine doesn't depend on the caller's PATH for it.
{
  runCommand,
  python3,
  git,
}:
runCommand "review-bot"
  {
    meta.description = "Automated Forgejo PR reviewer (review-bot identity) — engine-agnostic review routine";
    meta.mainProgram = "review-bot-review";
  }
  ''
    install -Dm755 ${./review.py} $out/bin/review-bot-review
    substituteInPlace $out/bin/review-bot-review \
      --replace-fail '@PYTHON@' ${python3}/bin/python3 \
      --replace-fail '@GIT@' ${git}/bin/git \
      --replace-fail '@REVIEW_PROMPT@' ${./review-prompt.md} \
      --replace-fail '@VERIFY_PROMPT@' ${./verify-prompt.md} \
      --replace-fail '@SYNTHESIS_PROMPT@' ${./synthesis-prompt.md} \
      --replace-fail '@TRIAGE_PROMPT@' ${./triage-prompt.md} \
      --replace-fail '@TRIAGE_VERIFY_PROMPT@' ${./triage-verify-prompt.md} \
      --replace-fail '@TRIAGE_SYNTHESIS_PROMPT@' ${./triage-synthesis-prompt.md}

    install -Dm755 ${./poll.py} $out/bin/review-bot-poll
    substituteInPlace $out/bin/review-bot-poll \
      --replace-fail '@PYTHON@' ${python3}/bin/python3
  ''
