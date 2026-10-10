"""``world`` — the one command. Every manual control the monitor and God have is here too."""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
import typer

from . import config as C

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Run and steer an Ambrosia world.")
knowledge_app = typer.Typer(help="The knowledge center.")
service_app = typer.Typer(help="Run this city at boot, hidden.")
app.add_typer(knowledge_app, name="knowledge")
app.add_typer(service_app, name="service")

STATE: dict = {"charter": None}


def charter() -> C.Charter:
    p = STATE["charter"] or os.environ.get("AMBROSIA_CHARTER") or "world.toml"
    if not Path(p).is_file():
        raise typer.BadParameter(f"no control sheet at {p} (pass --charter or run from the world's folder)")
    return C.load(p)


@app.callback()
def _root(charter_path: Optional[Path] = typer.Option(None, "--charter", "-c", help="path to world.toml")):
    if charter_path:
        STATE["charter"] = str(charter_path)


def _monitor(ch: C.Charter, city: str | None = None) -> str:
    spec = ch.city(city or C.whoami(ch))
    host = "127.0.0.1" if spec.monitor_bind in ("0.0.0.0", "127.0.0.1") else spec.monitor_bind
    return f"http://{host}:{spec.monitor_port}"


def _call(ch: C.Charter, path: str, body: dict | None = None) -> dict:
    url = _monitor(ch) + path
    try:
        r = httpx.post(url, json=body, timeout=900) if body is not None else httpx.get(url, timeout=30)
    except httpx.HTTPError:
        raise typer.Exit(_err(f"this city is not running ({url}); start it with `world up`"))
    return r.json()


def _err(msg: str) -> int:
    typer.secho(msg, fg="red", err=True)
    return 1


# -- running ------------------------------------------------------------------
@app.command()
def up(city: Optional[str] = typer.Option(None, help="override which city this machine is"),
       monitor: bool = typer.Option(True, help="serve the monitor")):
    """Start this machine's city: road station, pipelines, economy, monitor (and heaven if active)."""
    from .runtime import CityRuntime
    ch = charter()
    rt = CityRuntime(ch, city)
    typer.secho(f"{ch.world.name} · city {rt.city} · monitor {_monitor(ch, rt.city)}"
                f"{' · heaven' if ch.heaven.city == rt.city else ''}", fg="cyan")
    rt.run(monitor=monitor)


@app.command()
def status(brief: bool = typer.Option(False, "--brief", "-b", help="one screen, for agents")):
    """What the world is doing. Works whether or not the city is running."""
    ch = charter()
    me = C.whoami(ch)
    live = None
    try:
        live = httpx.get(_monitor(ch, me) + "/api/world", timeout=3).json()
    except httpx.HTTPError:
        pass
    from .store import Store
    db = ch.data_dir(me) / "city.db"
    st = Store(db, me)
    jobs = {r["status"]: r["n"] for r in st.query("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
    amb = st.one("SELECT COUNT(*) n FROM events WHERE kind='ambrosia.accepted'")["n"]
    last = st.query("SELECT kind, subject, ts FROM events WHERE kind NOT IN "
                    "('tick','agent.moved','road.in','road.flow','model.call') ORDER BY id DESC LIMIT 8")
    parks = st.query("SELECT provider, until FROM parks WHERE until>?", (time.time(),))
    out = {"world": ch.world.name, "city": me, "running": bool(live), "mode": ch.world.mode,
           "heaven": ch.heaven.city, "jobs": jobs, "ambrosia": amb,
           "parked": {p["provider"]: time.strftime("%m-%d %H:%M", time.localtime(p["until"])) for p in parks},
           "peers": {k: ("up" if v["up"] else "down") for k, v in (live or {}).get("peers", {}).items()},
           "recent": [f"{time.strftime('%H:%M', time.localtime(e['ts']))} {e['kind']} {e['subject'] or ''}"
                      for e in last]}
    if brief:
        typer.echo(json.dumps(out, separators=(",", ":")))
        return
    for k, v in out.items():
        typer.echo(f"{k:>9}: {v}")


@app.command()
def doctor():
    """Check everything this city needs, without changing anything."""
    ch = charter()
    me = C.whoami(ch)
    spec = ch.city(me)
    ok = True

    def line(good: bool, what: str, hint: str = ""):
        nonlocal ok
        ok &= good
        typer.secho(f"  {'ok ' if good else 'BAD'} {what}" + (f"  -> {hint}" if hint and not good else ""),
                    fg="green" if good else "red")

    typer.echo(f"{ch.world.name}: this machine is city {me!r}" + (" (heaven)" if ch.heaven.city == me else ""))
    line(me in [c.id for c in ch.cities], "city is declared in world.toml",
         "undeclared cities still work; declare it to give it buildings and a name")
    line(bool(os.environ.get(ch.road.token_env)), f"road token {ch.road.token_env} is set",
         "`world token` prints a new one; put it in the env file on every city")
    for pid, p in ch.providers.items():
        if p.kind in ("muse_cli", "claude_cli"):
            from .llm import _resolve_exe
            line(bool(_resolve_exe(p)), f"provider {pid}: executable found", "install it or fix executable_globs")
        elif p.kind == "openai_compat":
            n = sum(1 for k in p.key_envs if os.environ.get(k))
            line(n > 0, f"provider {pid}: {n}/{len(p.key_envs)} keys set", "set the key env vars")
    from .bootstrap import port_open
    for hub in ch.hubs():
        if hub.id == me:
            continue
        if hub.transport == "ssh":
            r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", hub.ssh or "", "true"],
                               capture_output=True, timeout=20)
            line(r.returncode == 0, f"hub {hub.id} reachable over ssh ({hub.ssh})", "check ssh keys")
        else:
            lp = ch.ports(hub.id)[1]
            line(port_open(hub.host or "", lp, 3), f"hub {hub.id} leaf port {hub.host}:{lp}",
                 "open the port on the hub's firewall or set transport = \"ssh\"")
    from . import buildings
    for mod in ch.plugins:
        sys.path.insert(0, str(ch.path.parent))
        try:
            __import__(mod)
            line(True, f"plugin {mod} imports")
        except Exception as e:
            line(False, f"plugin {mod} imports", f"{type(e).__name__}: {e}")
    for bid, b in ch.buildings_in(me).items():
        line(b.type in buildings.REGISTRY, f"building {bid} type {b.type} registered")
    raise typer.Exit(0 if ok else 1)


# -- steering -----------------------------------------------------------------
@app.command()
def commission(pipeline: str, subject: str):
    """Start a job on the running city."""
    typer.echo(json.dumps(_call(charter(), "/api/command",
                                {"op": "commission", "args": {"pipeline": pipeline, "subject": subject}})))


@app.command()
def command(op: str, args: str = typer.Argument("{}", help="JSON object")):
    """Send any command (pause, resume, retry, tick, add_peer) to the running city."""
    typer.echo(json.dumps(_call(charter(), "/api/command", {"op": op, "args": json.loads(args)})))


@app.command()
def god(message: str):
    """Speak to God. Changes that need approval come back as pending: answer with `world approve`."""
    r = _call(charter(), "/api/god", {"message": message})
    typer.echo(json.dumps(r, indent=1, default=str))


@app.command()
def approve(answer: str = typer.Argument(..., help="yes or no")):
    r = _call(charter(), "/api/god/approve", {"yes": answer.lower().startswith("y")})
    typer.echo(json.dumps(r, indent=1, default=str))


@app.command()
def token():
    """Print a fresh road token (put the same one on every city)."""
    from .runtime import new_token
    typer.echo(new_token())


# -- knowledge ----------------------------------------------------------------
@knowledge_app.command("index")
def k_index():
    from .knowledge import KnowledgeCenter
    from .store import Store
    ch = charter()
    me = C.whoami(ch)
    kc = KnowledgeCenter(Store(ch.data_dir(me) / "city.db", me), ch.knowledge.vault_paths)
    typer.echo(json.dumps(kc.index_vaults()))


@knowledge_app.command("search")
def k_search(query: str, k: int = 5):
    from .knowledge import KnowledgeCenter
    from .store import Store
    ch = charter()
    me = C.whoami(ch)
    kc = KnowledgeCenter(Store(ch.data_dir(me) / "city.db", me), ch.knowledge.vault_paths)
    for r in kc.search(query, k):
        typer.echo(f"{r['score']:.2f}  {r['title']}  {r['snippet'][:140]}")


# -- service ------------------------------------------------------------------
@service_app.command("install")
def svc_install(entry: Path = typer.Option(..., help="the world.py that starts this world")):
    """Start this city at boot (systemd user unit on Linux, hidden scheduled task on Windows)."""
    ch = charter()
    me = C.whoami(ch)
    name = f"ambrosia-{ch.world.name}-{me}"
    entry = entry.resolve()
    py = sys.executable
    if platform.system() == "Linux":
        unit = Path.home() / ".config/systemd/user" / f"{name}.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(f"""[Unit]
Description=Ambrosia world {ch.world.name}, city {me}
After=network-online.target

[Service]
WorkingDirectory={entry.parent}
Environment=PATH={os.environ.get("PATH", "")}
Environment=PYTHONIOENCODING=utf-8
ExecStart={shutil.which('python3') or py} {entry} up
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
""")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{name}.service"], check=False)
        typer.echo(f"installed {unit} (enable linger for boot without login: loginctl enable-linger)")
    elif platform.system() == "Windows":
        vbs = ch.data_dir(me) / f"{name}.vbs"
        pyw = Path(py).with_name("pythonw.exe")
        vbs.write_text(f'CreateObject("WScript.Shell").Run """{pyw if pyw.exists() else py}"" ""{entry}"" up", 0, False\r\n')
        # A per-user logon trigger needs no elevation through the ScheduledTasks module
        # (schtasks /SC ONLOGON does). Hidden: wscript runs pythonw with window style 0.
        ps = (f"$a = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument '\"{vbs}\"'; "
              "$t = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME; "
              "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
              "-ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew; "
              f"Register-ScheduledTask -TaskName '{name}' -Action $a -Trigger $t -Settings $s "
              f"-Description 'Ambrosia world {ch.world.name}, city {me} (hidden)' -Force | Out-Null")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True)
        if r.returncode:
            raise typer.Exit(_err(r.stderr.strip()[-400:]))
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        f"Start-ScheduledTask -TaskName '{name}'"], check=False)
        typer.echo(f"scheduled task {name} runs {entry} hidden at logon, and is running now")
    else:
        raise typer.Exit(_err("only Linux and Windows are supported"))


@service_app.command("uninstall")
def svc_uninstall():
    ch = charter()
    name = f"ambrosia-{ch.world.name}-{C.whoami(ch)}"
    if platform.system() == "Linux":
        subprocess.run(["systemctl", "--user", "disable", "--now", f"{name}.service"], check=False)
        (Path.home() / ".config/systemd/user" / f"{name}.service").unlink(missing_ok=True)
    elif platform.system() == "Windows":
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                        f"Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false"], check=False)
    typer.echo(f"removed {name}")


def main(charter_path: str | os.PathLike[str] | None = None) -> None:
    if charter_path:
        STATE["charter"] = str(charter_path)
    app()


if __name__ == "__main__":
    main()
