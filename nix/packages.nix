# nix/packages.nix — Rhemify package built with uv2nix
{ inputs, ... }: {
  perSystem = { pkgs, system, ... }:
    let
      rhemifyVenv = pkgs.callPackage ./python.nix {
        inherit (inputs) uv2nix pyproject-nix pyproject-build-systems;
      };

      # Import bundled skills, excluding runtime caches
      bundledSkills = pkgs.lib.cleanSourceWith {
        src = ../skills;
        filter = path: _type:
          !(pkgs.lib.hasInfix "/index-cache/" path);
      };

      runtimeDeps = with pkgs; [
        nodejs_20 ripgrep git openssh ffmpeg
      ];

      runtimePath = pkgs.lib.makeBinPath runtimeDeps;
    in {
      packages.default = pkgs.stdenv.mkDerivation {
        pname = "rhemify";
        version = "0.1.0";

        dontUnpack = true;
        dontBuild = true;
        nativeBuildInputs = [ pkgs.makeWrapper ];

        installPhase = ''
          runHook preInstall

          mkdir -p $out/share/rhemify $out/bin
          cp -r ${bundledSkills} $out/share/rhemify/skills

          ${pkgs.lib.concatMapStringsSep "\n" (name: ''
            makeWrapper ${rhemifyVenv}/bin/${name} $out/bin/${name} \
              --suffix PATH : "${runtimePath}" \
              --set RHEMIFY_BUNDLED_SKILLS $out/share/rhemify/skills
          '') [ "rhemify" "rhemify" "rhemify-acp" ]}

          runHook postInstall
        '';

        meta = with pkgs.lib; {
          description = "AI agent with advanced tool-calling capabilities";
          homepage = "https://github.com/AaronTan11/rhemify";
          mainProgram = "rhemify";
          license = licenses.mit;
          platforms = platforms.unix;
        };
      };
    };
}
