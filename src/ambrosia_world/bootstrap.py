"""Infrastructure: the NATS server each city runs, its config, tunnels and discovery.

Topology (NATS leaf nodes with JetStream domains):

* every city runs its own ``nats-server`` with JetStream, domain = city id,
  client port bound to 127.0.0.1 only. A city therefore works in isolation:
  its journal, queues and outbox are local.
* a **hub** city (any city with an open port) listens for leaf connections;
  every other city dials the hub as a leaf node. Interest propagates both
  ways over that one connection, so only the hub needs an open port.
  Firewalled machines need nothing inbound.
* ``transport = "ssh"`` dials the hub through ``ssh -L`` instead, for a hub
  whose firewall only admits ssh.

Adding a city is: put its IP in ``world.toml`` (or let mDNS find the hub) and
run ``python world.py up`` on it.
"""
from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

import httpx

from .config import Charter, CitySpec

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# -- child lifetime -----------------------------------------------------------
# A station or tunnel must die with its city, or a killed city leaves a
# nats-server holding the port. Windows: a Job Object with KILL_ON_JOB_CLOSE
# (the handle closes when this process dies, however it dies). Linux:
# PR_SET_PDEATHSIG.
_JOB = None


def _bind_to_me(proc: subprocess.Popen) -> None:
    global _JOB
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE

        class Basic(ctypes.Structure):
            _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                        ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD),
                        ("f", ctypes.c_size_t), ("g", wintypes.DWORD), ("h", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", ctypes.c_uint64 * 6), ("i", ctypes.c_size_t),
                        ("j", ctypes.c_size_t), ("k", ctypes.c_size_t), ("l", ctypes.c_size_t)]
        if _JOB is None:
            job = k32.CreateJobObjectW(None, None)
            info = Extended()
            info.Basic.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            _JOB = job
        h = k32.OpenProcess(0x0101, False, proc.pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        k32.AssignProcessToJobObject(_JOB, h)
        k32.CloseHandle(h)
    except Exception:
        pass


def _die_with_parent() -> None:          # preexec_fn on Linux
    try:
        import ctypes
        ctypes.CDLL("libc.so.6").prctl(1, 15)
    except Exception:
        pass


def _spawn(argv: list[str], **kw) -> subprocess.Popen:
    if os.name != "nt" and platform.system() == "Linux":
        kw["preexec_fn"] = _die_with_parent
    p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, **kw)
    _bind_to_me(p)
    return p



def _asset(version: str) -> tuple[str, str]:
    osname = {"Windows": "windows", "Linux": "linux", "Darwin": "darwin"}[platform.system()]
    arch = {"x86_64": "amd64", "AMD64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine(), "amd64")
    ext = "zip" if osname in ("windows", "darwin") else "tar.gz"
    return f"nats-server-v{version}-{osname}-{arch}", ext


def ensure_nats(version: str, bin_dir: Path) -> Path:
    """Download the pinned nats-server release once, verified against its SHA256SUMS."""
    exe = bin_dir / ("nats-server.exe" if os.name == "nt" else "nats-server")
    stamp = bin_dir / "nats-server.version"
    if exe.is_file() and stamp.is_file() and stamp.read_text().strip() == version:
        return exe
    found = shutil.which("nats-server")
    if found and _version_of(found) == version:
        return Path(found)
    bin_dir.mkdir(parents=True, exist_ok=True)
    stem, ext = _asset(version)
    base = f"https://github.com/nats-io/nats-server/releases/download/v{version}"
    with httpx.Client(follow_redirects=True, timeout=120) as http:
        blob = http.get(f"{base}/{stem}.{ext}").raise_for_status().content
        sums = http.get(f"{base}/SHA256SUMS")
    want = None
    if sums.status_code == 200:
        for line in sums.text.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == f"{stem}.{ext}":
                want = parts[0]
    got = hashlib.sha256(blob).hexdigest()
    if want and want != got:
        raise RuntimeError(f"nats-server download failed verification ({got} != {want})")
    name = exe.name
    if ext == "zip":
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            member = next(m for m in z.namelist() if m.endswith("/" + name) or m == name)
            exe.write_bytes(z.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as t:
            member = next(m for m in t.getmembers() if m.name.endswith("/" + name))
            exe.write_bytes(t.extractfile(member).read())
    exe.chmod(0o755)
    stamp.write_text(version)
    return exe


def _version_of(exe: str) -> str | None:
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.strip().split("v")[-1] if "v" in out else None


def port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Tunnel:
    """``ssh -N -L`` to a hub's leaf port; restarted by the caller if it dies."""

    def __init__(self, alias: str, remote_port: int, local_port: int):
        self.alias, self.remote_port, self.local_port = alias, remote_port, local_port
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        if self.proc and self.proc.poll() is None:
            return
        self.proc = _spawn(
            ["ssh", "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
             "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3",
             "-L", f"127.0.0.1:{self.local_port}:127.0.0.1:{self.remote_port}", self.alias],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            if port_open("127.0.0.1", self.local_port, 0.25):
                return
            if self.proc.poll() is not None:
                break
            time.sleep(0.25)

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


def nats_config(ch: Charter, me: CitySpec, token: str, store_dir: Path,
                hub_urls: list[str]) -> str:
    w = ch.world.name
    client, leaf_port, http = ch.ports(me.id)
    leaf: list[str] = []
    if me.hub:
        leaf += [f'  listen: "0.0.0.0:{leaf_port}"',
                 f'  authorization {{ user: "{w}", password: "{token}" }}']
    if hub_urls:
        urls = ", ".join(f'"{u}"' for u in hub_urls)
        leaf += [f"  remotes: [ {{ urls: [ {urls} ] }} ]", "  reconnect: 2s"]
    sd = str(store_dir).replace("\\", "/")
    return "\n".join([
        f'server_name: "{w}-{me.id}"',
        f'listen: "127.0.0.1:{client}"',
        f'http: "127.0.0.1:{http}"',
        "max_payload: 8MB",
        f'authorization {{ token: "{token}" }}',
        f'jetstream {{ store_dir: "{sd}", domain: "{me.id}", max_memory_store: 64MB, max_file_store: 4GB }}',
        *(["leafnodes {", *leaf, "}"] if leaf else []),
        "",
    ])


def hub_urls(ch: Charter, me: CitySpec, token: str, tunnels: dict[str, Tunnel]) -> list[str]:
    """Leaf remotes for this city: every hub except itself, direct or through a tunnel."""
    w = ch.world.name
    urls = []
    for hub in ch.hubs():
        if hub.id == me.id:
            continue
        port = ch.ports(hub.id)[1]
        if hub.transport == "ssh" and hub.ssh:
            t = tunnels.get(hub.id) or Tunnel(hub.ssh, port, free_port())
            tunnels[hub.id] = t
            t.start()
            urls.append(f"nats://{w}:{token}@127.0.0.1:{t.local_port}")
        elif hub.host:
            urls.append(f"nats://{w}:{token}@{hub.host}:{port}")
    return urls


def kill_pid(pid: int) -> None:
    """Stop a nats-server we started earlier (only if that pid is still a nats-server)."""
    if pid <= 0:
        return
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                                 capture_output=True, text=True, timeout=10).stdout
            if "nats-server" in out:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)
        else:
            exe = os.readlink(f"/proc/{pid}/exe") if os.path.exists(f"/proc/{pid}/exe") else ""
            if "nats-server" in exe:
                os.kill(pid, 15)
    except (OSError, subprocess.TimeoutExpired):
        pass


def start_nats(exe: Path, conf: Path, log: Path) -> subprocess.Popen:
    fh = open(log, "ab")
    return _spawn([str(exe), "-c", str(conf)], stdout=fh, stderr=subprocess.STDOUT)


# -- LAN discovery (mDNS) ----------------------------------------------------
SERVICE = "_ambrosia._tcp.local."


def advertise(ch: Charter, me: CitySpec):
    """Announce this city on the LAN. Returns the Zeroconf handle (close it on exit) or None."""
    try:
        from zeroconf import ServiceInfo, Zeroconf
        ip = _lan_ip()
        info = ServiceInfo(SERVICE, f"{ch.world.name}-{me.id}.{SERVICE}",
                           addresses=[socket.inet_aton(ip)], port=ch.road.leaf_port,
                           properties={"world": ch.world.name, "city": me.id, "hub": str(me.hub)})
        zc = Zeroconf()
        zc.register_service(info)
        return zc
    except Exception:
        return None


def discover(world: str, seconds: float = 3.0) -> list[dict]:
    """Cities of this world visible on the LAN right now."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return []
    found: list[dict] = []
    zc = Zeroconf()

    class L:
        def add_service(self, z, t, name):
            info = z.get_service_info(t, name, 2000)
            if not info:
                return
            props = {k.decode(): (v or b"").decode() for k, v in info.properties.items()}
            if props.get("world") == world:
                found.append({"city": props.get("city"), "hub": props.get("hub") == "True",
                              "host": socket.inet_ntoa(info.addresses[0]), "port": info.port})

        def update_service(self, *a):
            pass

        def remove_service(self, *a):
            pass

    ServiceBrowser(zc, SERVICE, L())
    time.sleep(seconds)
    zc.close()
    return found


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def lan_ip() -> str:
    return _lan_ip()


def python_exe() -> str:
    return sys.executable
