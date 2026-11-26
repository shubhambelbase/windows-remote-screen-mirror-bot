"""Unit tests for CloudflareTunnel parsing/lifecycle and MainRunner supervisor."""

from unittest.mock import patch

from execution.tunnel import CloudflareTunnel
from execution import main as runner_main


def test_tunnel_line_parsing_and_status():
    t = CloudflareTunnel(port=8765, cloudflared_path="cloudflared", protocol="http2")
    t._parse_line("INF |  https://alpha-bravo-charlie.trycloudflare.com                         |")
    t._parse_line("INF Registered tunnel connection connIndex=0 event=0 ip=198.41.192.167 location=sin04")

    assert t.public_url == "https://alpha-bravo-charlie.trycloudflare.com"
    assert t.edge_location == "sin04"
    assert t.edge_ip == "198.41.192.167"
    assert t.connection_count == 1

    st = t.get_status()
    assert st["protocol"] == "http2"
    assert st["running"] is False


def test_main_check_single_instance_overwrites_stale_pid(tmp_path):
    test_pid = tmp_path / ".bot.pid"
    test_pid.write_text("99999999", encoding="utf-8")

    with patch.object(runner_main, "PID_FILE", test_pid):
        assert runner_main.check_single_instance() is True
        assert test_pid.read_text(encoding="utf-8").strip() != "99999999"
