"""Console dashboard and console commands.

Type a command and press Enter (e.g. `add 1234567 50`). Press Enter on an empty
line to pause the dashboard while you type. `help` lists all commands.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import sys
import threading
import time
from collections import deque

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__
from .commands import HELP, CommandError, Commands
from .engine import Engine

log = logging.getLogger(__name__)

BANNER = r"""
 _     _           _ _           _   ____        _
| |   (_)_ __ ___ (_) |_ ___  __| | / ___| _ __ (_)_ __   ___ _ __
| |   | | '_ ` _ \| | __/ _ \/ _` | \___ \| '_ \| | '_ \ / _ \ '__|
| |___| | | | | | | | ||  __/ (_| |  ___) | | | | | |_) |  __/ |
|_____|_|_| |_| |_|_|\__\___|\__,_| |____/|_| |_|_| .__/ \___|_|
                                                  |_|"""


def run_command(cmds: Commands, line: str) -> list[str]:
    try:
        parts = shlex.split(line)
    except ValueError as e:
        return [f"error: {e}"]
    if not parts:
        return []
    name, args = parts[0].lower().lstrip("/!"), parts[1:]
    try:
        if name in ("help", "?"):
            return [f"{h.name} {h.args}".ljust(46) + h.text for h in HELP] + ["quit".ljust(46) + "Stop the sniper"]
        if name == "stats":
            return [f"{k:<18} {v}" for k, v in cmds.stats().items()]
        if name in ("list", "items"):
            rows = cmds.list_items()
            return [f"{i:<16} {p:>7} R$  limit {l or '-':<4} {n}" for i, n, p, l in rows] or ["Watchlist is empty"]
        if name == "add":
            _need(args, 2, "add <id|link> <max price> [buy limit]")
            return [cmds.add(args[0], args[1], args[2] if len(args) > 2 else None)]
        if name == "mass_add":
            _need(args, 2, "mass_add <max price> <id> <id> ...")
            return [cmds.mass_add(args[0], args[1:])]
        if name == "remove":
            _need(args, 1, "remove <id|link>")
            return [cmds.remove(args[0])]
        if name == "remove_all":
            return [cmds.remove_all()]
        if name == "price":
            _need(args, 2, "price <id|link> <max price>")
            return [cmds.price(args[0], args[1])]
        if name == "price_all":
            _need(args, 1, "price_all <max price>")
            return [cmds.price_all(args[0])]
        if name == "limit":
            _need(args, 2, "limit <id|link> <n|none>")
            return [cmds.limit(args[0], args[1])]
        if name == "focus":
            _need(args, 2, "focus <id|link> <max price>")
            return [cmds.focus(args[0], args[1])]
        if name == "unfocus":
            return [cmds.unfocus()]
        if name == "pause":
            return [cmds.pause()]
        if name == "resume":
            return [cmds.resume()]
        if name == "dry_run":
            _need(args, 1, "dry_run <on|off>")
            return [cmds.dry_run(args[0])]
        if name == "history":
            return cmds.history(args[0] if args else 10)
        if name == "lanes":
            return cmds.lanes()
        if name == "restart":
            return [cmds.restart()]
        if name in ("quit", "exit", "stop"):
            cmds.e.request_stop()
            return ["Stopping..."]
        return [f"unknown command '{name}' - type help"]
    except CommandError as e:
        return [f"error: {e}"]
    except Exception as e:  # config write errors etc.
        log.exception("command failed")
        return [f"error: {e}"]


def _need(args: list[str], n: int, usage: str) -> None:
    if len(args) < n:
        raise CommandError(f"usage: {usage}")


class ConsoleUI:
    def __init__(self, engine: Engine, cmds: Commands, dashboard: bool):
        self.e = engine
        self.cmds = cmds
        self.dashboard = dashboard and sys.stdout.isatty()
        self.console = Console()
        self.output: deque[str] = deque(maxlen=8)
        self.lines: asyncio.Queue[str] = asyncio.Queue()
        self.prompting = False

    def _reader(self, loop: asyncio.AbstractEventLoop) -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(self.lines.put_nowait, line.rstrip("\r\n"))

    def render(self) -> Group:
        stats = self.cmds.stats()
        info = Table.grid(padding=(0, 2))
        info.add_column(style="bold cyan", justify="right")
        info.add_column()
        info.add_column(style="bold cyan", justify="right")
        info.add_column()
        keys = list(stats)
        half = (len(keys) + 1) // 2
        for left, right in zip(keys[:half], keys[half:] + [""]):
            info.add_row(left, stats[left], right, stats.get(right, ""))

        events = Text()
        for ts, level, msg in list(self.e.stats.events)[-10:]:
            style = {"ERROR": "red", "WARNING": "yellow"}.get(level, "white")
            if msg.startswith("BOUGHT"):
                style = "bold green"
            events.append(time.strftime("%H:%M:%S ", time.localtime(ts)), style="dim")
            events.append(msg + "\n", style=style)

        out = Text("\n".join(self.output) or "type 'help' + Enter for commands", style="dim")
        return Group(
            Text(BANNER, style="bold magenta"),
            Panel(info, title=f"Limited Sniper v{__version__}", border_style="magenta"),
            Panel(events, title="Events", border_style="blue"),
            Panel(out, title="Console", border_style="grey50"),
        )

    async def _handle(self, line: str) -> None:
        for out in run_command(self.cmds, line):
            self.output.append(out)
            if not self.dashboard:
                self.console.print(out, highlight=False)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        if sys.stdin and sys.stdin.isatty():
            threading.Thread(target=self._reader, args=(loop,), daemon=True).start()

        if not self.dashboard:
            while True:
                await self._handle(await self.lines.get())

        with Live(self.render(), console=self.console, auto_refresh=False, screen=False, transient=False) as live:
            while True:
                try:
                    line = await asyncio.wait_for(self.lines.get(), 0.5)
                except asyncio.TimeoutError:
                    live.update(self.render(), refresh=True)
                    continue
                if line.strip() == "" and not self.prompting:
                    self.prompting = True
                    live.stop()
                    self.console.print("[bold]sniper>[/bold] ", end="")
                    continue
                await self._handle(line)
                if self.prompting:
                    self.prompting = False
                    live.start()
                live.update(self.render(), refresh=True)
