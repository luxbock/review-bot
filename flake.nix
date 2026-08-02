{
  description = "review-bot — automated Forgejo PR reviewer (review-bot identity)";

  # Deliberately nixpkgs-ONLY, and deliberately NOT how the deployment builds
  # this. nixos-config pins review-bot as a `flake = false` source input and
  # callPackages ./default.nix with the HOST's nixpkgs (pkgs/default.nix), which
  # is what keeps the service on one coherent package set; adding this file
  # changes nothing there.
  #
  # What it exists for is every OTHER context — a local checkout, and above all
  # an agent-vm worker. Without it the only way to build the package from inside
  # the repo was
  #   nix-build --expr 'with import <nixpkgs> {}; callPackage ./default.nix {}'
  # an ambient NIX_PATH channel lookup: it warns in a VM, is one NIX_PATH
  # difference from failing, and can silently evaluate a DIFFERENT nixpkgs. A
  # gate whose verdict depends on the ambient environment is not a gate, so a
  # worker was not allowed to run it and the packaged build could only be proven
  # host-side, after the run was over. That mattered here more than most:
  # default.nix does eleven `--replace-fail` substitutions against @…@
  # placeholders in review.py, and `--replace-fail` is a hard error when its
  # placeholder is gone — a break the test suite cannot see, because the tests
  # load review.py with the placeholders still in place.
  #
  # So the gates a worker runs are now, from the repo root:
  #   nix build .#default          — the packaged artifact
  #   nix flake check              — the same, plus the full test suite
  #   nix develop --command ...    — the project toolchain, exhaustively
  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      perSystem =
        pkgs:
        let
          reviewBot = pkgs.callPackage ./default.nix { };
        in
        {
          packages = {
            review-bot = reviewBot;
            default = reviewBot;
          };

          checks = {
            review-bot-tests = reviewBot.passthru.tests;
            default = reviewBot.passthru.tests;
          };

          # The suite's own inputs (exhaustive by design — see default.nix) plus the
          # tools a human or a worker reaches for while working ON the repo rather
          # than running its tests. A worker should need nothing from the host
          # beyond this shell.
          devShells.default = pkgs.mkShell {
            packages = reviewBot.passthru.testInputs ++ [
              pkgs.curl # poking the serve socket (`curl --unix-socket`) when debugging
              pkgs.jq # review.py journals one-line JSON; reading it by hand needs this
              pkgs.nixfmt # the repo now carries .nix files
            ];
          };

          # Bare `nixfmt` reads STDIN when given no path, so a plain
          # `formatter = nixfmt-rfc-style` makes an argumentless `nix fmt` fail on
          # empty input. Default to every .nix file in the tree instead. (nixos-config
          # solves this with treefmt-nix; that would mean a second flake input, and
          # this repo has exactly two .nix files.)
          formatter = pkgs.writeShellApplication {
            name = "nixfmt-repo";
            runtimeInputs = [
              pkgs.nixfmt
              pkgs.findutils
            ];
            text = ''
              if [ "$#" -eq 0 ]; then
                find . -name '*.nix' -not -path './.git/*' -exec nixfmt {} +
              else
                nixfmt "$@"
              fi
            '';
          };
        };
    in
    {
      packages = forAllSystems (pkgs: (perSystem pkgs).packages);
      checks = forAllSystems (pkgs: (perSystem pkgs).checks);
      devShells = forAllSystems (pkgs: (perSystem pkgs).devShells);
      formatter = forAllSystems (pkgs: (perSystem pkgs).formatter);
    };
}
