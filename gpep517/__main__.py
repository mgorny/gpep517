import argparse
import functools
import importlib
import json
import os
import pathlib
import sys
import sysconfig


ALL_OPT_LEVELS = [0, 1, 2]
DEFAULT_PREFIX = "/usr"
DEFAULT_FALLBACK_BACKEND = "setuptools.build_meta:__legacy__"


def get_toml(path):
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}


def get_backend(args):
    with os.fdopen(args.output_fd, "w") as out:
        print(get_toml(args.pyproject_toml)
              .get("build-system", {})
              .get("build-backend", ""),
              file=out)
    return 0


def build_wheel(args):
    build_sys = get_toml(args.pyproject_toml).get("build-system", {})
    backend_s = args.backend
    if backend_s is None:
        backend_s = build_sys.get("build-backend", args.fallback_backend)
        if backend_s is None:
            raise RuntimeError(
                "pyproject.toml is missing or does not specify build-backend "
                "and --no-fallback-backend specified")
    package, _, obj = backend_s.partition(":")

    if not args.allow_compressed:
        import zipfile
        orig_open = zipfile.ZipFile.open
        orig_write = zipfile.ZipFile.write
        orig_writestr = zipfile.ZipFile.writestr

        @functools.wraps(zipfile.ZipFile.open)
        def override_open(self, name, mode="r", pwd=None,
                          *, force_zip64=False):
            if mode == "w":
                if not isinstance(name, zipfile.ZipInfo):
                    name = zipfile.ZipInfo(name)
                name.compress_type = zipfile.ZIP_STORED
            ret = orig_open(self, name, mode, pwd, force_zip64=force_zip64)
            return ret

        @functools.wraps(zipfile.ZipFile.write)
        def override_write(self, filename, arcname=None,
                           compress_type=None, compresslevel=None):
            return orig_write(self, filename, arcname, zipfile.ZIP_STORED)

        @functools.wraps(zipfile.ZipFile.writestr)
        def override_writestr(self, zinfo_or_arcname, data,
                              compress_type=None, compresslevel=None):
            return orig_writestr(self, zinfo_or_arcname, data,
                                 zipfile.ZIP_STORED)

        zipfile.ZipFile.open = override_open
        zipfile.ZipFile.write = override_write
        zipfile.ZipFile.writestr = override_writestr

    def safe_samefile(path, cwd):
        try:
            return cwd.samefile(path)
        except Exception:
            return False

    orig_modules = frozenset(sys.modules)
    orig_path = list(sys.path)
    # strip the current directory from sys.path
    cwd = pathlib.Path.cwd()
    sys.path = [x for x in sys.path if not safe_samefile(x, cwd)]
    sys.path[:0] = build_sys.get("backend-path", [])
    backend = importlib.import_module(package)

    if obj:
        for name in obj.split("."):
            backend = getattr(backend, name)

    wheel_name = backend.build_wheel(args.wheel_dir, args.config_json)

    for mod in frozenset(sys.modules).difference(orig_modules):
        del sys.modules[mod]
    sys.path = orig_path

    if not args.allow_compressed:
        zipfile.ZipFile.open = orig_open
        zipfile.ZipFile.write = orig_write
        zipfile.ZipFile.writestr = orig_writestr

    with os.fdopen(args.output_fd, "w") as out:
        print(wheel_name, file=out)
    return 0


def install_scheme_dict(prefix, dist_name):
    ret = sysconfig.get_paths(vars={"base": prefix,
                                    "platbase": prefix})
    # header path hack copied from installer's __main__.py
    ret["headers"] = os.path.join(
        sysconfig.get_path("include", vars={"installed_base": prefix}),
        dist_name)
    # end of copy-paste
    return ret


def parse_optimize_arg(val):
    spl = val.split(",")
    if "all" in spl:
        return ALL_OPT_LEVELS
    return [int(x) for x in spl]


def install_wheel(args):
    from installer import install
    from installer.destinations import SchemeDictionaryDestination
    from installer.sources import WheelFile
    from installer.utils import get_launcher_kind

    with WheelFile.open(args.wheel) as source:
        dest = SchemeDictionaryDestination(
            install_scheme_dict(args.prefix, source.distribution),
            args.interpreter,
            get_launcher_kind(),
            bytecode_optimization_levels=args.optimize,
            destdir=args.destdir,
        )
        install(source, dest, {})

    return 0


def verify_pyc(args):
    from gpep517.qa import qa_verify_pyc

    install_dict = install_scheme_dict(args.prefix, "")
    sitedirs = frozenset(install_dict[x] for x in ("purelib", "platlib"))
    result = qa_verify_pyc(args.destdir, sitedirs)

    def fpath(p):
        if p.startswith("/"):
            return "/" + os.path.relpath(p, args.destdir)
        return p

    for kind, entries in result.items():
        for e in sorted(entries):
            print(f"{kind}:{':'.join(fpath(x) for x in e)}")
    return 1 if any(v for v in result.values()) else 0


def add_install_path_args(parser):
    group = parser.add_argument_group("install paths")
    group.add_argument("--destdir",
                       help="Staging directory for the install (it will "
                       "be prepended to all paths)",
                       required=True)
    group.add_argument("--prefix",
                       default=DEFAULT_PREFIX,
                       help="Prefix to install to "
                       f"(default: {DEFAULT_PREFIX})")


def main(argv=sys.argv):
    argp = argparse.ArgumentParser(prog=argv[0])

    subp = argp.add_subparsers(dest="command",
                               required=True)

    parser = subp.add_parser("get-backend",
                             help="Print build-backend from pyproject.toml")
    parser.add_argument("--output-fd",
                        default=1,
                        help="FD to use for output (default: 1)",
                        type=int)
    parser.add_argument("--pyproject-toml",
                        default="pyproject.toml",
                        help="Path to pyproject.toml file")

    parser = subp.add_parser("build-wheel",
                             help="Build wheel using specified backend")
    parser.add_argument("--backend",
                        help="Backend to use (defaults to reading "
                             "from pyproject.toml)")
    parser.add_argument("--fallback-backend",
                        default=DEFAULT_FALLBACK_BACKEND,
                        help="Backend to use if pyproject.toml does not exist "
                        "or does not specify one "
                        f"(default: {DEFAULT_FALLBACK_BACKEND})")
    parser.add_argument("--no-fallback-backend",
                        action="store_const",
                        dest="fallback_backend",
                        const=None,
                        help="Disable backend fallback (i.e. require backend "
                        "declaration in pyproject.toml")
    parser.add_argument("--config-json",
                        help="JSON-encoded dictionary of config_settings "
                             "to pass to the build backend",
                        type=json.loads)
    parser.add_argument("--allow-compressed",
                        help="Allow creating compressed zipfiles (gpep517 "
                        "will attempt to patch compression out by default)",
                        action="store_true")
    parser.add_argument("--output-fd",
                        help="FD to output the wheel name to",
                        required=True,
                        type=int)
    parser.add_argument("--pyproject-toml",
                        default="pyproject.toml",
                        help="Path to pyproject.toml file")
    parser.add_argument("--wheel-dir",
                        help="Directory to output the wheel into",
                        required=True)

    parser = subp.add_parser("install-wheel",
                             help="Install wheel")
    add_install_path_args(parser)
    parser.add_argument("--interpreter",
                        default=sys.executable,
                        help="The interpreter to put in script shebangs "
                        f"(default: {sys.executable})")
    parser.add_argument("--optimize",
                        type=parse_optimize_arg,
                        default=[],
                        help="Comma-separated list of optimization levels "
                        "to compile bytecode for (default: none), pass 'all' "
                        "to enable all known optimization levels (currently: "
                        f"{', '.join(str(x) for x in ALL_OPT_LEVELS)})")
    parser.add_argument("wheel",
                        help="Wheel to install")

    parser = subp.add_parser("verify-pyc",
                             help="Verify that all installed modules were "
                                  "byte-compiled and there are no stray .pyc "
                                  "files")
    add_install_path_args(parser)

    args = argp.parse_args(argv[1:])

    func = globals()[args.command.replace("-", "_")]
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
