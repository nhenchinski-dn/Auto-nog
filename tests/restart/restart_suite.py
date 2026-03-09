#!/usr/bin/env python3
"""
Restart test suite for DNOS nodes.

Runs warm/cold restart cycles for empty/normal/scale configs, measures
restart time, checks config consistency, and verifies reboot markers.

Uses MCP to fetch the device configuration when enabled.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple


DEFAULT_EMPTY_ITERS = 10
DEFAULT_OTHER_ITERS = 3
DEFAULT_MAX_WAIT = 600
DEFAULT_COOLDOWN = 5
DEFAULT_THRESHOLD = 0.30


@dataclass
class RestartIterationResult:
    iteration: int
    restart_command: str
    issued_at: str
    restart_seconds: Optional[float]
    node_back_online: bool
    reboot_verified: bool
    config_unchanged: Optional[bool]
    notes: List[str]


@dataclass
class ScenarioResult:
    name: str
    iterations: List[RestartIterationResult]


class MCPClient:
    def __init__(self, server_url: str, timeout: int = 30):
        self.server_url = server_url
        self.timeout = timeout
        self._next_id = 1
        self._sse = None
        self._post_url = None
        self._sock = None
        self._connect()

    def _connect(self) -> None:
        req = urllib.request.Request(
            self.server_url,
            headers={"Accept": "text/event-stream"},
        )
        self._sse = urllib.request.urlopen(req, timeout=self.timeout)
        try:
            if hasattr(self._sse, "fp") and hasattr(self._sse.fp, "raw"):
                self._sock = getattr(self._sse.fp.raw, "_sock", None)
            if not self._sock and hasattr(self._sse, "fp"):
                self._sock = getattr(self._sse.fp, "_sock", None)
            if self._sock is not None:
                self._sock.settimeout(self.timeout)
        except Exception:
            self._sock = None
        start = time.time()
        while time.time() - start < self.timeout:
            event, data = self._read_event()
            if event == "endpoint":
                self._post_url = self._parse_endpoint(data)
                break
        if not self._post_url:
            self._post_url = self._fallback_post_url()
        if not self._post_url:
            raise RuntimeError("Failed to resolve MCP POST endpoint")

    def _fallback_post_url(self) -> Optional[str]:
        if self.server_url.endswith("/sse"):
            return self.server_url[:-4] + "/message"
        return None

    def _parse_endpoint(self, data: str) -> Optional[str]:
        try:
            payload = json.loads(data)
            if isinstance(payload, dict) and "url" in payload:
                return payload["url"]
        except json.JSONDecodeError:
            pass
        if data.startswith("http"):
            return data
        return None

    def _read_event(self) -> Tuple[str, str]:
        event = None
        data_lines: List[str] = []
        while True:
            if self._sock:
                ready, _, _ = select.select([self._sock], [], [], self.timeout)
                if not ready:
                    raise RuntimeError("Timed out waiting for MCP SSE event")
            else:
                fp = getattr(self._sse, "fp", None)
                if fp and hasattr(fp, "fileno"):
                    ready, _, _ = select.select([fp], [], [], self.timeout)
                    if not ready:
                        raise RuntimeError("Timed out waiting for MCP SSE event")
            line = self._sse.readline()
            if not line:
                raise RuntimeError("MCP SSE stream closed unexpectedly")
            text = line.decode("utf-8").rstrip("\n")
            if not text:
                break
            if text.startswith("event:"):
                event = text.split(":", 1)[1].strip()
            elif text.startswith("data:"):
                data_lines.append(text.split(":", 1)[1].strip())
        return (event or "message"), "\n".join(data_lines)

    def _send_request(self, payload: Dict) -> int:
        req_id = payload.get("id", self._next_id)
        self._next_id = max(self._next_id + 1, req_id + 1)
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._post_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=self.timeout).read()
        return req_id

    def _wait_for_response(self, req_id: int) -> Dict:
        start = time.time()
        while time.time() - start < self.timeout:
            _, data = self._read_event()
            if not data:
                continue
            try:
                message = json.loads(data)
            except json.JSONDecodeError:
                continue
            if message.get("id") != req_id:
                continue
            return message
        raise RuntimeError("Timed out waiting for MCP response")

    def list_tools(self) -> List[Dict]:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "tools/list",
            "params": {},
        }
        req_id = self._send_request(payload)
        message = self._wait_for_response(req_id)
        if "error" in message:
            raise RuntimeError(json.dumps(message["error"]))
        result = message.get("result", {})
        tools = result.get("tools", [])
        if isinstance(tools, list):
            return tools
        return []

    def call_tool(self, tool_name: str, arguments: Dict) -> Dict:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }
        req_id = self._send_request(payload)
        message = self._wait_for_response(req_id)
        if "error" in message:
            raise RuntimeError(json.dumps(message["error"]))
        return message.get("result", {})


class MCPConfigFetcher:
    def __init__(self, server_url: str, device_name: str, timeout: int = 30):
        self.device_name = device_name
        self.client = MCPClient(server_url=server_url, timeout=timeout)
        self.tool_name, self.device_arg = self._select_config_tool()

    def _select_config_tool(self) -> Tuple[str, str]:
        tools = self.client.list_tools()
        selected = None
        device_arg = None
        for tool in tools:
            name = tool.get("name")
            if not isinstance(name, str):
                continue
            if "get_device_config" not in name:
                continue
            schema = tool.get("inputSchema", {}) if isinstance(tool, dict) else {}
            props = schema.get("properties", {}) if isinstance(schema, dict) else {}
            if not isinstance(props, dict):
                props = {}
            for candidate in ("device_name", "device", "target", "node", "system"):
                if candidate in props:
                    device_arg = candidate
                    break
            selected = name
            break
        if not selected or not device_arg:
            raise RuntimeError(
                "Failed to auto-detect MCP get_device_config tool. "
                "Check MCP server tools/list output."
            )
        return selected, device_arg

    def get_config(self) -> str:
        result = self.client.call_tool(
            self.tool_name, {self.device_arg: self.device_name}
        )
        content = result.get("content")
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
            return "\n".join(texts).strip()
        if isinstance(content, str):
            return content.strip()
        return json.dumps(result)


class SSHRunner:
    def __init__(self, host: str, username: Optional[str], password: Optional[str]):
        self.host = host
        self.username = username
        self.password = password
        self.sshpass_available = shutil.which("sshpass") is not None
        if self.password and not self.sshpass_available:
            raise RuntimeError("sshpass is required for password-based SSH.")

    def _build_ssh_command(self, command: str) -> List[str]:
        target = f"{self.username}@{self.host}" if self.username else self.host
        ssh_cmd = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=10",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=1",
            target,
            command,
        ]
        if self.password:
            return ["sshpass", "-p", self.password] + ssh_cmd
        return ssh_cmd

    def run(self, command: str, timeout: int = 30) -> Tuple[str, int]:
        cmd = self._build_ssh_command(command)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (result.stdout or "") + (result.stderr or "")
            return output.strip(), result.returncode
        except subprocess.TimeoutExpired:
            return "Command timed out", 124
        except Exception as exc:
            return f"Command error: {exc}", 1


def resolve_mcp_url(config_path: str, server_name: str) -> Optional[str]:
    try:
        with open(config_path, "r") as handle:
            data = json.load(handle)
        servers = data.get("mcpServers", {})
        server = servers.get(server_name, {})
        return server.get("url")
    except (OSError, json.JSONDecodeError):
        return None


def parse_boot_marker(uptime_output: str) -> Optional[str]:
    for line in uptime_output.splitlines():
        if line.startswith("System Start Time:"):
            return line.strip()
    return None


def diff_config_summary(before: str, after: str) -> Tuple[bool, List[str]]:
    if before.strip() == after.strip():
        return True, []
    before_lines = set(before.strip().splitlines())
    after_lines = set(after.strip().splitlines())
    added = sorted(after_lines - before_lines)
    removed = sorted(before_lines - after_lines)
    notes = []
    if added:
        notes.append(f"Added lines: {len(added)}")
    if removed:
        notes.append(f"Removed lines: {len(removed)}")
    return False, notes


def wait_for_offline(runner: SSHRunner, timeout: int) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        _, exit_code = runner.run("show system uptime", timeout=10)
        if exit_code != 0:
            return True
        time.sleep(2)
    return False


def wait_for_online(runner: SSHRunner, timeout: int) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        _, exit_code = runner.run("show system uptime", timeout=10)
        if exit_code == 0:
            return True
        time.sleep(3)
    return False


def mean(values: List[float]) -> Optional[float]:
    if not values:
        return None
    return sum(values) / len(values)


def build_restart_command(
    warm: bool,
    use_ncp: bool,
    ncp_id: int,
    warm_override: Optional[str],
    cold_override: Optional[str],
) -> str:
    if warm and warm_override:
        return warm_override
    if not warm and cold_override:
        return cold_override
    if use_ncp:
        base = f"request system restart ncp {ncp_id}"
        return f"{base} warm" if warm else base
    return "request system restart warm" if warm else "request system restart"


def apply_config(runner: SSHRunner, name: str, command: Optional[str]) -> bool:
    if not command:
        print(f"ℹ No config command provided for {name}; skipping apply step")
        return True
    print(f"Applying config for {name}")
    output, exit_code = runner.run(command, timeout=120)
    if exit_code != 0:
        print(f"✗ Failed to apply {name} config: {output}")
        return False
    print(f"✓ Config applied for {name}")
    return True


def run_scenario(
    scenario_name: str,
    iterations: int,
    warm: bool,
    runner: SSHRunner,
    config_fetcher: Optional[MCPConfigFetcher],
    warm_override: Optional[str],
    cold_override: Optional[str],
    use_ncp: bool,
    ncp_id: int,
    max_wait: int,
    cooldown: int,
    config_snapshot: Optional[str],
) -> ScenarioResult:
    results: List[RestartIterationResult] = []
    for i in range(1, iterations + 1):
        notes: List[str] = []
        pre_uptime_out, pre_uptime_code = runner.run("show system uptime", timeout=20)
        pre_boot_marker = (
            parse_boot_marker(pre_uptime_out) if pre_uptime_code == 0 else None
        )
        if pre_uptime_code != 0:
            notes.append("Failed to capture pre-restart uptime marker")
        restart_cmd = build_restart_command(
            warm=warm,
            use_ncp=use_ncp,
            ncp_id=ncp_id,
            warm_override=warm_override,
            cold_override=cold_override,
        )
        issued_at = datetime.utcnow().isoformat() + "Z"
        start = time.monotonic()
        output, exit_code = runner.run(restart_cmd, timeout=20)
        if exit_code != 0:
            notes.append(f"Restart command exit {exit_code}: {output}")

        offline_seen = wait_for_offline(runner, timeout=min(120, max_wait))
        if not offline_seen:
            notes.append("Node did not go offline before online wait")

        online = wait_for_online(runner, timeout=max_wait)
        restart_seconds = time.monotonic() - start if online else None

        reboot_verified = False
        config_unchanged = None

        uptime_out, uptime_code = runner.run("show system uptime", timeout=20)
        if uptime_code == 0:
            current_marker = parse_boot_marker(uptime_out) or uptime_out[:120]
            if config_snapshot is not None:
                current_config = (
                    config_fetcher.get_config()
                    if config_fetcher
                    else runner.run("show configuration", timeout=60)[0]
                )
                config_unchanged, diff_notes = diff_config_summary(
                    config_snapshot, current_config
                )
                notes.extend(diff_notes)
            if pre_boot_marker and current_marker and current_marker != pre_boot_marker:
                reboot_verified = True
        else:
            notes.append("Failed to read system uptime after restart")

        results.append(
            RestartIterationResult(
                iteration=i,
                restart_command=restart_cmd,
                issued_at=issued_at,
                restart_seconds=restart_seconds,
                node_back_online=online,
                reboot_verified=reboot_verified,
                config_unchanged=config_unchanged,
                notes=notes,
            )
        )
        if i < iterations:
            time.sleep(cooldown)
    return ScenarioResult(name=scenario_name, iterations=results)


def main() -> int:
    parser = argparse.ArgumentParser(description="DNOS restart test suite")
    parser.add_argument("--device", required=True, help="Device name or IP")
    parser.add_argument("--username", help="SSH username")
    parser.add_argument("--password", help="SSH password (requires sshpass)")
    parser.add_argument("--use-ncp", action="store_true", help="Use NCP restart commands")
    parser.add_argument("--ncp-id", type=int, default=0, help="NCP id")
    parser.add_argument("--mcp-config", action="store_true", help="Use MCP to fetch config")
    parser.add_argument("--mcp-server", default="network-mapper", help="MCP server name")
    parser.add_argument("--mcp-config-path", default="/home/dn/mcp.json", help="MCP config path")
    parser.add_argument("--mcp-timeout", type=int, default=10, help="MCP timeout seconds")
    parser.add_argument(
        "--mcp-fallback",
        action="store_true",
        help="Continue without MCP if MCP connection fails",
    )
    parser.add_argument("--max-wait", type=int, default=DEFAULT_MAX_WAIT, help="Max wait for node online")
    parser.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN, help="Seconds between iterations")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD, help="Time change threshold ratio")
    parser.add_argument("--warm-restart-cmd", help="Override warm restart command")
    parser.add_argument("--cold-restart-cmd", help="Override cold restart command")
    parser.add_argument("--empty-config-cmd", help="Command to apply empty config")
    parser.add_argument("--normal-config-cmd", help="Command to apply normal config")
    parser.add_argument("--scale-config-cmd", help="Command to apply scale config")
    args = parser.parse_args()

    runner = SSHRunner(args.device, args.username, args.password)

    config_fetcher = None
    if args.mcp_config:
        mcp_url = resolve_mcp_url(args.mcp_config_path, args.mcp_server)
        if not mcp_url:
            print("✗ Failed to resolve MCP server URL")
            return 2
        print(f"Connecting to MCP server {mcp_url}...")
        try:
            config_fetcher = MCPConfigFetcher(
                server_url=mcp_url, device_name=args.device, timeout=args.mcp_timeout
            )
        except Exception as exc:
            if args.mcp_fallback:
                print(f"⚠ MCP unavailable: {exc}")
                print("Continuing with SSH 'show configuration' fallback.")
                config_fetcher = None
            else:
                print(f"✗ MCP connection failed: {exc}")
                print("Use --mcp-fallback or omit --mcp-config to continue.")
                return 2

    scenarios = []

    scenario_defs = [
        ("warm_empty", True, DEFAULT_EMPTY_ITERS, args.empty_config_cmd),
        ("warm_normal", True, DEFAULT_OTHER_ITERS, args.normal_config_cmd),
        ("warm_scale", True, DEFAULT_OTHER_ITERS, args.scale_config_cmd),
        ("cold_empty", False, DEFAULT_EMPTY_ITERS, args.empty_config_cmd),
        ("cold_normal", False, DEFAULT_OTHER_ITERS, args.normal_config_cmd),
        ("cold_scale", False, DEFAULT_OTHER_ITERS, args.scale_config_cmd),
    ]

    report = {
        "device": args.device,
        "started_at": datetime.utcnow().isoformat() + "Z",
        "scenarios": [],
        "time_threshold_ratio": args.threshold,
        "notes": [
            "Verify console output manually for reboot confirmation.",
            "Restart commands may prompt for confirmation in interactive CLI.",
        ],
    }

    baseline_times: Dict[str, List[float]] = {"warm": [], "cold": []}

    for name, warm, iterations, config_cmd in scenario_defs:
        print("=" * 64)
        print(f"SCENARIO: {name}")
        print("=" * 64)

        if not apply_config(runner, name, config_cmd):
            print(f"✗ Skipping {name} due to config apply failure")
            continue

        config_snapshot = None
        if config_fetcher:
            config_snapshot = config_fetcher.get_config()
        else:
            config_snapshot = runner.run("show configuration", timeout=60)[0]

        scenario_result = run_scenario(
            scenario_name=name,
            iterations=iterations,
            warm=warm,
            runner=runner,
            config_fetcher=config_fetcher,
            warm_override=args.warm_restart_cmd,
            cold_override=args.cold_restart_cmd,
            use_ncp=args.use_ncp,
            ncp_id=args.ncp_id,
            max_wait=args.max_wait,
            cooldown=args.cooldown,
            config_snapshot=config_snapshot,
        )
        report["scenarios"].append(
            {
                "name": name,
                "iterations": [asdict(i) for i in scenario_result.iterations],
            }
        )
        times = [
            i.restart_seconds
            for i in scenario_result.iterations
            if i.restart_seconds is not None
        ]
        if name.endswith("empty"):
            baseline_times["warm" if warm else "cold"].extend(times)
        scenarios.append(scenario_result)

    # Compare to baseline
    comparisons = []
    warm_baseline = mean(baseline_times["warm"])
    cold_baseline = mean(baseline_times["cold"])
    for scenario in scenarios:
        is_warm = scenario.name.startswith("warm")
        baseline = warm_baseline if is_warm else cold_baseline
        if baseline is None:
            continue
        times = [i.restart_seconds for i in scenario.iterations if i.restart_seconds]
        avg = mean(times)
        if avg is None:
            continue
        ratio = abs(avg - baseline) / baseline if baseline else None
        comparisons.append(
            {
                "scenario": scenario.name,
                "baseline_seconds": baseline,
                "average_seconds": avg,
                "ratio": ratio,
                "within_threshold": ratio is not None and ratio <= args.threshold,
            }
        )
    report["comparisons"] = comparisons
    report["finished_at"] = datetime.utcnow().isoformat() + "Z"

    report_name = f"restart_suite_report_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json"
    with open(report_name, "w") as handle:
        json.dump(report, handle, indent=2)
    print(f"\nReport written to {report_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
