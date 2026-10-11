"""Optional: run your own Tor as a normal child process and use it as proxies.

Unlike the old sniper this does not ship a tor.exe, does not need admin rights
and does not install a Windows service. Download the official "Tor Expert
Bundle" from https://www.torproject.org/download/tor/ and point
Proxies.Tor.Path at tor.exe (or set it to "tor" if Tor is on your PATH).

Each HTTPTunnelPort gets its own circuit, so N ports = N different exit IPs.
Note: Roblox rate limits / blocks many Tor exits; private proxies or alt
accounts usually work better.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile

log = logging.getLogger(__name__)


class TorProcess:
    def __init__(self, path: str, instances: int, start_port: int):
        self.path = path
        self.instances = instances
        self.start_port = start_port
        self.proc: asyncio.subprocess.Process | None = None
        self.tmpdir: str | None = None

    @property
    def proxies(self) -> list[str]:
        return [f"http://127.0.0.1:{self.start_port + i}" for i in range(self.instances)]

    async def start(self, timeout: float = 120) -> list[str]:
        exe = shutil.which(self.path) or self.path
        if not os.path.isfile(exe):
            raise FileNotFoundError(f"Tor not found at '{self.path}'. Set Proxies.Tor.Path to tor.exe from the Tor Expert Bundle.")
        self.tmpdir = tempfile.mkdtemp(prefix="limited-sniper-tor-")
        torrc = os.path.join(self.tmpdir, "torrc")
        lines = [
            f"DataDirectory {os.path.join(self.tmpdir, 'data')}",
            "SocksPort 0",
            "ClientOnly 1",
            "Log notice stdout",
        ]
        lines += [f"HTTPTunnelPort 127.0.0.1:{self.start_port + i}" for i in range(self.instances)]
        with open(torrc, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        self.proc = await asyncio.create_subprocess_exec(
            exe, "-f", torrc, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        log.info("Started Tor (pid %s) with %d tunnel ports from %d", self.proc.pid, self.instances, self.start_port)

        async def wait_bootstrap() -> None:
            assert self.proc and self.proc.stdout
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    raise RuntimeError("Tor exited during startup")
                text = line.decode(errors="replace").strip()
                if "Bootstrapped" in text:
                    log.info("Tor: %s", text.split("]", 1)[-1].strip())
                if "Bootstrapped 100%" in text:
                    return
                if "[err]" in text:
                    raise RuntimeError(f"Tor error: {text}")

        try:
            await asyncio.wait_for(wait_bootstrap(), timeout)
        except BaseException:
            await self.stop()
            raise
        asyncio.get_running_loop().create_task(self._drain())
        return self.proxies

    async def _drain(self) -> None:
        if self.proc and self.proc.stdout:
            while await self.proc.stdout.readline():
                pass

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 10)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self.tmpdir:
            shutil.rmtree(self.tmpdir, ignore_errors=True)
