# review-bot — the reusable PR-review routine (subtask #3 core).
#
# A stdlib-only Python program that runs the portable review prompt on a
# selectable engine (claude / codex), then posts ONE Markdown comment to a
# Forgejo PR as the read-only review-bot identity (REST + token, never `fj`).
# See review.py and notes/decisions/forgejo-multi-identity.md.
#
# Since the serve/client split, credentials live only with the service:
#   bin/review-bot-serve        — inetd-style service entry point (systemd
#                                 socket unit, Accept=yes); imports the review
#                                 module below and owns all credentials.
#   bin/review-bot-review       — thin CLIENT on caller PATHs: same argv as
#                                 before, speaks JSON/NDJSON over the Unix
#                                 socket at $REVIEW_BOT_SOCKET; holds no creds.
#   bin/review-bot-review-local — the in-process implementation (direct
#                                 execution; requires local forge token +
#                                 CLAUDE_CONFIG_DIR/CODEX_HOME). Symlink to
#                                 lib/review-bot/review.py, which is also what
#                                 review-bot-serve imports.
#   bin/review-bot-poll         — mention poller; dispatches through the client
#                                 so every review serializes at the socket.
#
# The harness binaries (`claude` / `codex`) are resolved from PATH at RUNTIME
# (of the SERVICE, post-split), not baked in. `git` IS baked in so the routine
# doesn't depend on the runtime PATH for it.
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
    # The in-process pipeline: installed once under lib/ so the serve entry point
    # imports exactly the code that review-bot-review-local executes.
    install -Dm755 ${./review.py} $out/lib/review-bot/review.py
    substituteInPlace $out/lib/review-bot/review.py \
      --replace-fail '@PYTHON@' ${python3}/bin/python3 \
      --replace-fail '@GIT@' ${git}/bin/git \
      --replace-fail '@REVIEW_PROMPT@' ${./review-prompt.md} \
      --replace-fail '@VERIFY_PROMPT@' ${./verify-prompt.md} \
      --replace-fail '@SYNTHESIS_PROMPT@' ${./synthesis-prompt.md} \
      --replace-fail '@TRIAGE_PROMPT@' ${./triage-prompt.md} \
      --replace-fail '@TRIAGE_VERIFY_PROMPT@' ${./triage-verify-prompt.md} \
      --replace-fail '@TRIAGE_SYNTHESIS_PROMPT@' ${./triage-synthesis-prompt.md}
    mkdir -p $out/bin
    ln -s ../lib/review-bot/review.py $out/bin/review-bot-review-local

    install -Dm755 ${./serve.py} $out/bin/review-bot-serve
    substituteInPlace $out/bin/review-bot-serve \
      --replace-fail '@PYTHON@' ${python3}/bin/python3 \
      --replace-fail '@REVIEW_IMPL@' $out/lib/review-bot/review.py

    install -Dm755 ${./client.py} $out/bin/review-bot-review
    substituteInPlace $out/bin/review-bot-review \
      --replace-fail '@PYTHON@' ${python3}/bin/python3

    install -Dm755 ${./poll.py} $out/bin/review-bot-poll
    substituteInPlace $out/bin/review-bot-poll \
      --replace-fail '@PYTHON@' ${python3}/bin/python3
  ''
