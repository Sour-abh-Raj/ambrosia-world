#!/usr/bin/env python3
"""Build and run this world with one file.

    python world.py up          start this machine's city (installs everything on first run)
    python world.py status      what the world is doing
    python world.py doctor      check this machine
    python world.py god "…"     speak to God

Needs only Python 3.11+ and uv (https://docs.astral.sh/uv/). The first run
creates .venv beside this file, installs the world into it and re-runs
itself there. On NixOS it also pins the C++ runtime that compiled wheels
need, behind a GC root so a garbage collection cannot break the world.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _nixos_libs() -> str | None:
    if not Path("/etc/NIXOS").exists():
        return None
    root = HERE / ".gcroots" / "libstdcxx"
    if not root.exists():
        root.parent.mkdir(exist_ok=True)
        subprocess.run(["nix", "build", "nixpkgs#stdenv.cc.cc.lib", "--out-link", str(root)], check=True)
    return str(root / "lib")


def bootstrap() -> None:
    if os.environ.get("AMBROSIA_BOOTSTRAPPED"):
        return
    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv is required: https://docs.astral.sh/uv/getting-started/installation/")
    subprocess.run([uv, "sync", "--quiet"], cwd=HERE, check=True)
    env = dict(os.environ, AMBROSIA_BOOTSTRAPPED="1", PYTHONIOENCODING="utf-8")
    lib = _nixos_libs()
    if lib:
        env["LD_LIBRARY_PATH"] = lib + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    argv = [str(PY), str(Path(__file__).resolve()), *sys.argv[1:]]
    if os.name == "nt":
        sys.exit(subprocess.run(argv, env=env).returncode)
    os.execve(str(PY), argv, env)


if __name__ == "__main__":
    bootstrap()
    sys.path.insert(0, str(HERE))
    from ambrosia_world.cli import main
    main(HERE / "world.toml")
