{
  # TODO/FIXME: Update this package to recent nixpkgs and borg
  description = "borg backup";

  inputs = {
    flake-utils.url = "github:numtide/flake-utils";
    devshell.url = "github:numtide/devshell";
  };

  outputs = { self, nixpkgs, flake-utils, devshell }:
  let
    inherit (nixpkgs.lib) substring composeManyExtensions;
    inherit (flake-utils.lib) eachDefaultSystem;

    version' = "0.1.0.dev0";
    localVersion = "${substring 0 8 (self.lastModifiedDate or self.lastModified or "19700101")}.${self.shortRev or "dirty"}";
  in
  {
    overlays = rec {
      borgthin = composeManyExtensions [
        (final: prev: (with prev; {
          borgthin = (python310Packages.buildPythonApplication rec {
            name = "borgthin";
            version = "${version'}+${localVersion}";
            format = "pyproject";
            src = ./.;

            SETUPTOOLS_SCM_PRETEND_VERSION = version;

            nativeBuildInputs = (with python310Packages; [
              pkgconfig
              cython
              setuptools-scm
            ]);
            buildInputs = [
              openssl
              lz4
              zstd
              xxHash
              acl
            ];
            propagatedBuildInputs = (with python310Packages; [
              (callPackage ./py-msgpack.nix { })
              packaging
              pyfuse3
              argon2-cffi
              (platformdirs.overrideAttrs (final: prev: rec {
                # TODO: remove this when nixpkgs updates platformdirs
                name = "${prev.pname}-${final.version}";
                version = "3.0.0";
                SETUPTOOLS_SCM_PRETEND_VERSION = version;
                src = fetchFromGitHub {
                  owner = prev.pname;
                  repo = prev.pname;
                  rev = "refs/tags/${version}";
                  hash = "sha256-RiZ26BGqS8nN5qHpUt7LIXSck/cGM8qlet3uL81TyPo";
                };
              }))
            ]);

            makeWrapperArgs = [
              ''--prefix PATH ':' "${openssh}/bin"''
            ];
            meta.mainProgram = "borgthin";
          });
        }))
      ];
      default = borgthin;
    };
  } // (eachDefaultSystem (system:
    let
      pkgs = import nixpkgs {
        inherit system;
        overlays = [
          devshell.overlay
          self.overlays.default
        ];
      };
    in
    {
      devShells.default = pkgs.devshell.mkShell {
        imports = [ "${pkgs.devshell.extraModulesDir}/language/c.nix" ];

        language.c = with pkgs; rec {
          compiler = gcc;
          libraries = [
            gcc.cc.lib
            openssl
            lz4
            zstd
            xxHash
            acl
            fuse
            fuse3
            msgpack
          ];
          includes = libraries;
        };

        packages = with pkgs; [
          (python310.withPackages (ps: with ps; [
            virtualenv
          ]))
          fakeroot
        ];

        devshell.startup = {
          venv.text = ''
            if [ ! -e borg-env ]; then
              virtualenv borg-env
              source borg-env/bin/activate
              pip install -r requirements.d/development.txt
              python setup.py -v develop
            else
              source borg-env/bin/activate
            fi
          '';
        };

        env = [
          {
            name = "LDFLAGS";
            eval = "-L\${DEVSHELL_DIR}/lib";
          }
        ];

        commands = [
          {
            name = "rebuild";
            help = "re-build the editable project";
            command = ''python setup.py -v develop'';
          }
        ];
      };

      packages = rec {
        inherit (pkgs) borgthin;
        default = borgthin;
      };
    }));
}
