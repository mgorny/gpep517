import argparse
import functools
import importlib
import json
import logging
import os
import pathlib
import sys
import sysconfig
import tempfile

from pathlib import Path


ALL_OPT_LEVELS = [0, 1, 2]
DEFAULT_PREFIX = Path("/usr")
DEFAULT_FALLBACK_BACKEND = "setuptools.build_meta:__legacy__"

logger = logging.getLogger("gpep517")


def get_toml(path: Path):
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib

    try:
        with path.open("rb") as f:
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


def build_wheel_impl(args, wheel_dir: Path):
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

    logger.info(f"Building wheel via backend {backend_s}")
    wheel_name = backend.build_wheel(str(wheel_dir), args.config_json)
    logger.info(f"The backend produced {wheel_dir / wheel_name}")

    for mod in frozenset(sys.modules).difference(orig_modules):
        del sys.modules[mod]
    sys.path = orig_path

    if not args.allow_compressed:
        zipfile.ZipFile.open = orig_open
        zipfile.ZipFile.write = orig_write
        zipfile.ZipFile.writestr = orig_writestr

    return wheel_name


def build_wheel(args):
    with os.fdopen(args.output_fd, "w") as out:
        print(build_wheel_impl(args, args.wheel_dir), file=out)
    return 0


def install_scheme_dict(prefix: Path, dist_name: str):
    ret = sysconfig.get_paths(vars={"base": str(prefix),
                                    "platbase": str(prefix)})
    # header path hack copied from installer's __main__.py
    ret["headers"] = os.path.join(
        sysconfig.get_path("include", vars={"installed_base": str(prefix)}),
        dist_name)
    # end of copy-paste
    return ret


def parse_optimize_arg(val):
    spl = val.split(",")
    if "all" in spl:
        return ALL_OPT_LEVELS
    return [int(x) for x in spl]


def install_wheel_impl(args, wheel: Path):
    from installer import install
    from installer.destinations import SchemeDictionaryDestination
    from installer.sources import WheelFile
    from installer.utils import get_launcher_kind

    with WheelFile.open(wheel) as source:
        dest = SchemeDictionaryDestination(
            install_scheme_dict(args.prefix, source.distribution),
            str(args.interpreter),
            get_launcher_kind(),
            bytecode_optimization_levels=args.optimize,
            destdir=str(args.destdir),
        )
        logger.info(f"Installing {wheel} into {args.destdir}")
        install(source, dest, {})
        logger.info("Installation complete")


def install_wheel(args):
    install_wheel_impl(args, args.wheel)

    return 0


def install_from_source(args):
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        wheel = build_wheel_impl(args, temp_path)
        install_wheel_impl(args, temp_path / wheel)

    return 0


def verify_pyc(args):
    from gpep517.qa import qa_verify_pyc

    install_dict = install_scheme_dict(args.prefix, "")
    sitedirs = frozenset(Path(install_dict[x]) for x in ("purelib", "platlib"))
    result = qa_verify_pyc(args.destdir, sitedirs)

    def fpath(p):
        if isinstance(p, Path):
            return str(p.root / p.relative_to(args.destdir))
        return p

    for kind, entries in result.items():
        for e in sorted(entries):
            print(f"{kind}:{':'.join(fpath(x) for x in e)}")
    return 1 if any(v for v in result.values()) else 0


def add_install_path_args(parser):
    group = parser.add_argument_group("install paths")
    group.add_argument("--destdir",
                       type=Path,
                       help="Staging directory for the install (it will "
                       "be prepended to all paths)",
                       required=True)
    group.add_argument("--prefix",
                       type=Path,
                       default=DEFAULT_PREFIX,
                       help="Prefix to install to "
                       f"(default: {DEFAULT_PREFIX})")


def add_build_args(parser):
    group = parser.add_argument_group("backend selection")
    group.add_argument("--backend",
                       help="Backend to use (defaults to reading "
                            "from pyproject.toml)")
    group.add_argument("--fallback-backend",
                       default=DEFAULT_FALLBACK_BACKEND,
                       help="Backend to use if pyproject.toml does not exist "
                       "or does not specify one "
                       f"(default: {DEFAULT_FALLBACK_BACKEND!r})")
    group.add_argument("--no-fallback-backend",
                       action="store_const",
                       dest="fallback_backend",
                       const=None,
                       help="Disable backend fallback (i.e. require backend "
                       "declaration in pyproject.toml")
    group.add_argument("--pyproject-toml",
                       type=Path,
                       default="pyproject.toml",
                       help="Path to pyproject.toml file (used only if "
                       "--backend is not specified)")

    group = parser.add_argument_group("build options")
    group.add_argument("--allow-compressed",
                       help="Allow creating compressed zipfiles (gpep517 "
                       "will attempt to patch compression out by default)",
                       action="store_true")
    group.add_argument("--config-json",
                       help="JSON-encoded dictionary of config_settings "
                            "to pass to the build backend",
                       type=json.loads)


def add_install_args(parser):
    add_install_path_args(parser)

    group = parser.add_argument_group("install options")
    group.add_argument("--interpreter",
                       type=Path,
                       default=sys.executable,
                       help="The interpreter to put in script shebangs "
                       f"(default: {sys.executable})")
    group.add_argument("--optimize",
                       type=parse_optimize_arg,
                       default=[],
                       help="Comma-separated list of optimization levels "
                       "to compile bytecode for (default: none), pass 'all' "
                       "to enable all known optimization levels (currently: "
                       f"{', '.join(str(x) for x in ALL_OPT_LEVELS)})")


def main(argv=sys.argv):
    argp = argparse.ArgumentParser(prog=argv[0])
    argp.add_argument("-q", "--quiet",
                      action="store_const",
                      dest="loglevel",
                      const=logging.WARNING,
                      default=logging.INFO,
                      help="Disable verbose progress reporting")

    subp = argp.add_subparsers(dest="command",
                               required=True)

    parser = subp.add_parser("get-backend",
                             help="Print build-backend from pyproject.toml")
    parser.add_argument("--output-fd",
                        default=1,
                        help="FD to use for output (default: 1)",
                        type=int)
    parser.add_argument("--pyproject-toml",
                        type=Path,
                        default="pyproject.toml",
                        help="Path to pyproject.toml file")

    parser = subp.add_parser("build-wheel",
                             help="Build wheel from sources")
    group = parser.add_argument_group("required arguments")
    group.add_argument("--output-fd",
                       help="FD to output the wheel name to",
                       required=True,
                       type=int)
    group.add_argument("--wheel-dir",
                       type=Path,
                       help="Directory to write the wheel into",
                       required=True)
    add_build_args(parser)

    parser = subp.add_parser("install-from-source",
                             help="Build and install wheel from sources "
                             "(without preserving the wheel)")
    add_build_args(parser)
    add_install_args(parser)

    parser = subp.add_parser("install-wheel",
                             help="Install the specified wheel")
    add_install_args(parser)
    parser.add_argument("wheel",
                        type=Path,
                        help="Wheel to install")

    parser = subp.add_parser("verify-pyc",
                             help="Verify that all installed modules were "
                                  "byte-compiled and there are no stray .pyc "
                                  "files")
    add_install_path_args(parser)

    args = argp.parse_args(argv[1:])
    logging.basicConfig(format="{asctime} {name} {levelname} {message}",
                        style="{",
                        level=args.loglevel)

    func = globals()[args.command.replace("-", "_")]
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
