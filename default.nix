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
#
# This file is the ONE package definition. nixos-config consumes it directly
# (`flake = false` input + callPackage in pkgs/default.nix, so the deployment
# builds it with the HOST's nixpkgs); ./flake.nix is a thin, nixpkgs-only entry
# point that callPackages this same file for local and in-VM work. Neither
# derives from the other — keep the build logic here.
{
  lib,
  runCommand,
  python3,
  git,
}:

let
  # Everything the SUITE needs, and nothing else — this list is consumed by the
  # deployment's callPackage too, so it stays free of dev ergonomics (those live
  # in flake.nix's devShell, which extends this). Exhaustive on purpose: a factory
  # VM worker gets the whole suite environment from `nix develop` alone, with no
  # host-provided extras and no ambient channel. The engines (`claude` / `codex`)
  # are deliberately absent — no test may reach a live model, so a suite that can
  # find one is a bug surface.
  testInputs = [
    python3 # the whole codebase is stdlib-only — no site-packages needed
    git # the suite builds real repos and worktrees; review.py shells out to it
  ];

  # The tests import review.py IN-TREE (placeholders patched at import time) and
  # resolve the prompt files relative to the repo root, so the check derivation
  # needs the real layout rather than a handful of copied scripts. Listed
  # explicitly: a new prompt file must be added here as well as to the
  # substituteInPlace list below, and forgetting shows up as a failing check.
  testTree = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [
      ./review.py
      ./client.py
      ./serve.py
      ./poll.py
      ./feedback.py
      ./review-prompt.md
      ./verify-prompt.md
      ./synthesis-prompt.md
      ./triage-prompt.md
      ./triage-verify-prompt.md
      ./triage-synthesis-prompt.md
      ./audit-prompt.md
      ./audit-verify-prompt.md
      ./audit-synthesis-prompt.md
      ./select-prompt.md
      # The suite asserts README prose against the code it describes (the diff-cap
      # doc-coupling test), so the check derivation needs it. A README edit therefore
      # rebuilds this check — which is the point: that is the drift being gated.
      ./README.md
      # Same reason as README.md: the suite pins the SKILL's engine-command gotcha
      # against the overrides review.py actually reads, so an edit to it must rebuild
      # this check rather than drift away from the code unnoticed.
      ./skills
      ./tests # includes tests/fixtures — the diff-packing goldens live there
      ./tools
    ];
  };

in
runCommand "review-bot"
  {
    meta.description = "Automated Forgejo PR reviewer (review-bot identity) — engine-agnostic review routine";
    meta.mainProgram = "review-bot-review";

    passthru = {
      inherit testInputs;

      # Every test file, run the way the repo runs them. This is what
      # `nix flake check` and the packaged-build gate share, so a worker can
      # prove the suite hermetically without a host round-trip.
      tests =
        runCommand "review-bot-tests"
          {
            nativeBuildInputs = testInputs;
          }
          ''
            cp -r ${testTree} src
            chmod -R u+w src
            cd src
            # git refuses to operate without an identity or a HOME to read.
            export HOME="$TMPDIR"
            export GIT_CONFIG_GLOBAL="$TMPDIR/gitconfig"
            git config --global user.name "review-bot tests"
            git config --global user.email "tests@example.invalid"
            git config --global init.defaultBranch main
            for t in tests/test_*.py; do
              echo "== $t"
              python3 "$t"
            done
            touch $out
          '';
    };
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
      --replace-fail '@TRIAGE_SYNTHESIS_PROMPT@' ${./triage-synthesis-prompt.md} \
      --replace-fail '@AUDIT_PROMPT@' ${./audit-prompt.md} \
      --replace-fail '@AUDIT_VERIFY_PROMPT@' ${./audit-verify-prompt.md} \
      --replace-fail '@AUDIT_SYNTHESIS_PROMPT@' ${./audit-synthesis-prompt.md} \
      --replace-fail '@SELECT_PROMPT@' ${./select-prompt.md}
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

    install -Dm755 ${./feedback.py} $out/bin/review-bot-feedback
    substituteInPlace $out/bin/review-bot-feedback \
      --replace-fail '@PYTHON@' ${python3}/bin/python3
  ''
