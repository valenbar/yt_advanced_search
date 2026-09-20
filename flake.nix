{
  description = "Python template";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    nix-dev.url = "github:valenbar/nix-dev";
  };

  outputs =
    {
      self,
      nixpkgs,
      flake-utils,
      nix-dev,
      ...
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };

        pythonPackages = pkgs.python314Packages;

        # dependencies needed in path during runtime
        runtimeDependencies = with pkgs; [ yt-dlp ];

        # dependencies needed during build time
        nativeBuildInputs = with pkgs; [
          pythonPackages.python
        ];

        # things that need to be linked against eg. openssl, dbus
        buildInputs = with pkgs; [ ];

        pyproject = (fromTOML (builtins.readFile ./pyproject.toml)).project;
        pname = pyproject.name;
        version = pyproject.version;
      in
      {
        devShells.default = pkgs.mkShell {
          packages =
            with pkgs;
            [
              # nix-dev.packages.${system}.helix-wrapped
              ruff
              pythonPackages.python-lsp-server
              basedpyright
            ]
            ++ nativeBuildInputs
            ++ runtimeDependencies;
        };

        packages.default = pythonPackages.python.pkgs.buildPythonApplication rec {
          inherit
            pname
            version
            nativeBuildInputs
            buildInputs
            ;

          format = "setuptools";

          src = ./.;

          doCheck = false;

          # wrap binary to include runtime dependencies in path
          postInstall = ''
            wrapProgram $out/bin/${pname} \
              --prefix PATH : ${pkgs.lib.makeBinPath runtimeDependencies}
          '';

          meta = {
            description = "";
            homepage = "";
            license = [ ];
            mainProgram = pname;
          };
        };
      }
    );
}
