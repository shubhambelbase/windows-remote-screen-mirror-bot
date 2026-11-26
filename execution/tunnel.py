"""Cloudflare Tunnel Supervisor & Multi-Transport Engine.

Spawns and monitors a cloudflared quick tunnel to expose the local HTTP/WebSocket server,
supporting configurable transport protocols (QUIC vs HTTP/2), health checks, and clean restarts.
"""

import logging
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

TUNNEL_REGEX = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
EDGE_REGEX = re.compile(r"Registered tunnel connection.*location=([a-zA-Z0-9]+)")
CONN_REGEX = re.compile(r"connIndex=\d+.*?\bip=([0-9.]+)")


class CloudflareTunnel:
    """Manages cloudflared subprocess, supports QUIC/HTTP2 transports, and tracks connection diagnostics."""

    def __init__(
        self,
        port: int = 8765,
        cloudflared_path: Optional[str] = None,
        protocol: str = "auto",  # 'auto', 'quic', or 'http2'
    ):
        self.port = port
        self.cloudflared_path = cloudflared_path or self._find_cloudflared()
        self.protocol = os.getenv("CLOUDFLARE_PROTOCOL", protocol).lower()
        self.process: Optional[subprocess.Popen] = None
        self.public_url: Optional[str] = None
        self.edge_location: Optional[str] = None
        self.edge_ip: Optional[str] = None
        self.connection_count: int = 0
        self.start_time: float = 0.0
        self.restart_count: int = 0

        self._url_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def _find_cloudflared(self) -> str:
        """Locates cloudflared executable across PATH, WinGet Links, and Program Files."""
        path = shutil.which("cloudflared")
        if path:
            return path
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        default_paths = [
            os.path.join(local_app_data, "Microsoft", "WinGet", "Links", "cloudflared.exe") if local_app_data else "",
            r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
            r"C:\Program Files\cloudflared\cloudflared.exe",
        ]
        for candidate in default_paths:
            if candidate and os.path.exists(candidate):
                return candidate
        return "cloudflared"

    def is_alive(self) -> bool:
        """Returns True if the underlying cloudflared subprocess is currently running."""
        return self.process is not None and self.process.poll() is None

    def start(self, timeout: float = 30.0) -> Optional[str]:
        """Starts cloudflared with the chosen protocol and verifies edge connection."""
        with self._lock:
            self._stop_event.clear()
            self._url_event.clear()
            self.public_url = None
            self.edge_location = None
            self.edge_ip = None
            self.connection_count = 0

            cmd = [self.cloudflared_path, "tunnel"]
            if self.protocol in ("quic", "http2"):
                cmd.extend(["--protocol", self.protocol])
            cmd.extend(["--url", f"http://127.0.0.1:{self.port}"])

            logger.info(f"Starting Cloudflare Tunnel [{self.protocol.upper()}]: {' '.join(cmd)}")
            self.start_time = time.monotonic()

            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except Exception as e:
                logger.error(f"Failed to launch cloudflared: {e}")
                self.process = None
                return None

            self._thread = threading.Thread(target=self._read_output, daemon=True)
            self._thread.start()

        self._url_event.wait(timeout=timeout)
        if self.public_url:
            elapsed = time.monotonic() - self.start_time
            logger.info(
                f"✅ Tunnel ready in {elapsed:.2f}s: {self.public_url} "
                f"[Edge: {self.edge_location or 'Unknown'}, Protocol: {self.protocol}]"
            )
            return self.public_url

        logger.warning("Cloudflare tunnel timed out waiting for public URL.")
        self.stop()
        return None

    def restart(self, timeout: float = 30.0) -> Optional[str]:
        """Stops any dead or stale tunnel process and starts a fresh tunnel."""
        self.stop()
        self.restart_count += 1
        return self.start(timeout=timeout)

    def _parse_line(self, line_str: str) -> None:
        """Parses a single cloudflared log line for URL, edge location, IP, and readiness."""
        match_url = TUNNEL_REGEX.search(line_str)
        if match_url and not self.public_url:
            self.public_url = match_url.group(0)
            threading.Timer(0.5, self._url_event.set).start()

        match_edge = EDGE_REGEX.search(line_str)
        if match_edge:
            self.edge_location = match_edge.group(1)

        match_ip = CONN_REGEX.search(line_str)
        if match_ip:
            self.edge_ip = match_ip.group(1)

        if self.public_url and (
            "Registered tunnel connection" in line_str
            or "connIndex=" in line_str
            or "ready" in line_str.lower()
        ):
            self.connection_count += 1
            self._url_event.set()

    def _read_output(self):
        """Continuously reads output lines to extract URL, edge location, and connection events."""
        proc = self.process
        if not proc or not proc.stdout:
            return

        try:
            for line in iter(proc.stdout.readline, ""):
                if self._stop_event.is_set():
                    break
                self._parse_line(line.strip())
        except Exception as e:
            logger.debug(f"Tunnel reader exception: {e}")
        finally:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass

    def get_status(self) -> Dict[str, Any]:
        """Returns tunnel status metrics."""
        return {
            "public_url": self.public_url,
            "protocol": self.protocol,
            "edge_location": self.edge_location,
            "edge_ip": self.edge_ip,
            "connections": self.connection_count,
            "restart_count": self.restart_count,
            "uptime_s": round(time.monotonic() - self.start_time, 1) if self.start_time and self.is_alive() else 0.0,
            "running": self.is_alive(),
        }

    def stop(self):
        """Terminates cloudflared process safely."""
        with self._lock:
            self._stop_event.set()
            proc = self.process
            self.process = None
            if proc:
                logger.info("Terminating Cloudflare Tunnel...")
                try:
                    proc.terminate()
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                    except Exception:
                        pass
            self.public_url = None
