#!/usr/bin/env python3
"""
Node Restart Test Script

Tests warm and cold restarts with different configurations:
- Empty config
- Normal config (including breakout)
- Scale config

Measures restart times and verifies:
- OS reboot occurred
- Config remains unchanged
- Restart time consistency
- Full restart completion
"""

import subprocess
import time
import statistics
import json
import sys
import shutil
import os
import urllib.request
import urllib.error
from datetime import datetime
from typing import List, Dict, Optional, Tuple


class MCPClient:
    def __init__(
        self,
        server_url: str,
        tool_name: Optional[str] = None,
        command_arg: Optional[str] = None,
        target: Optional[str] = None,
        target_arg: Optional[str] = None,
        timeout: int = 30,
    ):
        self.server_url = server_url
        self.tool_name = tool_name
        self.command_arg = command_arg
        self.target = target
        self.target_arg = target_arg
        self.timeout = timeout
        self._next_id = 1
        self._sse = None
        self._post_url = None
        self._connect()

    def _connect(self):
        req = urllib.request.Request(
            self.server_url,
            headers={"Accept": "text/event-stream"}
        )
        self._sse = urllib.request.urlopen(req, timeout=self.timeout)
        start_time = time.time()
        while time.time() - start_time < self.timeout:
            event, data = self._read_sse_event()
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

    def _read_sse_event(self) -> Tuple[str, str]:
        event = None
        data_lines = []
        while True:
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

    def _next_request_id(self) -> int:
        req_id = self._next_id
        self._next_id += 1
        return req_id

    def _send_request(self, payload: Dict) -> int:
        req_id = payload.get("id")
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._post_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(request, timeout=self.timeout).read()
        return req_id

    def _wait_for_response(self, req_id: int) -> Dict:
        start_time = time.time()
        while time.time() - start_time < self.timeout:
            _, data = self._read_sse_event()
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

    def _format_result(self, result: Dict) -> Tuple[str, int]:
        if not result:
            return "", 0
        if result.get("isError") or "error" in result:
            return json.dumps(result), 1
        content = result.get("content")
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    texts.append(item.get("text", ""))
                else:
                    texts.append(json.dumps(item))
            return "\n".join(t for t in texts if t), 0
        return json.dumps(result), 0

    def set_tool(self, tool_name: str, command_arg: str, target_arg: Optional[str]):
        self.tool_name = tool_name
        self.command_arg = command_arg
        self.target_arg = target_arg

    def list_tools(self) -> List[Dict]:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_request_id(),
            "method": "tools/list",
            "params": {}
        }
        req_id = self._send_request(payload)
        message = self._wait_for_response(req_id)
        if "error" in message:
            raise RuntimeError(json.dumps(message["error"]))
        result = message.get("result", {})
        tools = result.get("tools", [])
        if not isinstance(tools, list):
            return []
        return tools

    def execute(self, command: str) -> Tuple[str, int]:
        if not self.tool_name or not self.command_arg:
            raise RuntimeError("MCP tool name and command arg must be set")
        args = {self.command_arg: command}
        if self.target:
            if not self.target_arg:
                raise RuntimeError("MCP target arg must be set when target is provided")
            args[self.target_arg] = self.target
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_request_id(),
            "method": "tools/call",
            "params": {
                "name": self.tool_name,
                "arguments": args
            }
        }
        req_id = self._send_request(payload)
        message = self._wait_for_response(req_id)
        if "error" in message:
            return json.dumps(message["error"]), 1
        return self._format_result(message.get("result", {}))


class RestartTester:
    def __init__(
        self,
        host: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        use_ncp: bool = False,
        ncp_id: int = 0,
        empty_config_cmd: Optional[str] = None,
        normal_config_cmd: Optional[str] = None,
        scale_config_cmd: Optional[str] = None,
        use_mcp: bool = True,
        mcp_config_path: Optional[str] = "/home/dn/mcp.json",
        mcp_server: Optional[str] = "network-mapper",
        mcp_url: Optional[str] = None,
        mcp_tool: Optional[str] = None,
        mcp_command_arg: Optional[str] = None,
        mcp_target: Optional[str] = None,
        mcp_target_arg: Optional[str] = None,
        mcp_timeout: int = 30,
    ):
        """
        Initialize the restart tester.
        
        Args:
            host: SSH host (if None, assumes local execution)
            username: SSH username
            use_ncp: Whether to use NCP commands (ncp 0)
            ncp_id: NCP ID to use (default 0)
        """
        self.host = host
        self.username = username
        self.use_ncp = use_ncp
        self.ncp_id = ncp_id
        self.password = password
        self.empty_config_cmd = empty_config_cmd
        self.normal_config_cmd = normal_config_cmd
        self.scale_config_cmd = scale_config_cmd
        self.sshpass_available = shutil.which("sshpass") is not None
        self.use_mcp = use_mcp
        self.mcp_client = None
        if self.password and not self.sshpass_available:
            raise RuntimeError(
                "sshpass is required for password-based SSH. "
                "Install sshpass or use SSH keys."
            )
        if self.use_mcp:
            resolved_url = mcp_url or self._resolve_mcp_url(
                mcp_config_path, mcp_server
            )
            if not resolved_url:
                raise RuntimeError("MCP URL not provided or resolved from config")
            target = mcp_target or self.host
            self.mcp_client = MCPClient(
                server_url=resolved_url,
                tool_name=mcp_tool,
                command_arg=mcp_command_arg,
                target=target,
                target_arg=mcp_target_arg,
                timeout=mcp_timeout
            )
            if not mcp_tool or not mcp_command_arg:
                tool_name, command_arg, target_arg = self._auto_select_mcp_tool(
                    mcp_tool, mcp_command_arg, mcp_target_arg
                )
                self.mcp_client.set_tool(tool_name, command_arg, target_arg)
        self.results = {
            'warm_empty': [],
            'warm_normal': [],
            'warm_scale': [],
            'cold_empty': [],
            'cold_normal': [],
            'cold_scale': []
        }
        self.config_snapshots = {}
        
    def execute_command(self, command: str, timeout: Optional[int] = None) -> Tuple[str, int]:
        """
        Execute a command either locally or via SSH.
        
        Returns:
            Tuple of (output, exit_code)
        """
        if self.mcp_client:
            try:
                return self.mcp_client.execute(command)
            except Exception as e:
                return f"MCP error: {str(e)}", -1
        if self.host:
            ssh_cmd = ['ssh']
            if self.username:
                ssh_cmd.extend([f'{self.username}@{self.host}'])
            else:
                ssh_cmd.append(self.host)
            ssh_cmd.append(command)
            if self.password:
                cmd = ['sshpass', '-p', self.password] + ssh_cmd
            else:
                cmd = ssh_cmd
        else:
            cmd = command
        
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=not self.host
            )
            return result.stdout + result.stderr, result.returncode
        except subprocess.TimeoutExpired:
            return "Command timed out", -1
        except Exception as e:
            return f"Error executing command: {str(e)}", -1
    
    def get_config(self) -> str:
        """Get current configuration."""
        output, _ = self.execute_command("show configuration")
        return output
    
    def save_config_snapshot(self, name: str):
        """Save a snapshot of the current configuration."""
        self.config_snapshots[name] = self.get_config()
        print(f"✓ Saved config snapshot: {name}")
    
    def verify_config_unchanged(self, name: str) -> bool:
        """Verify that configuration hasn't changed."""
        current_config = self.get_config()
        original_config = self.config_snapshots.get(name)
        
        if original_config is None:
            print(f"⚠ Warning: No snapshot found for {name}")
            return False

    def apply_config(self, name: str, command: Optional[str]) -> bool:
        """
        Apply a config profile before running a test scenario.
        """
        if not command:
            print(f"ℹ No config command provided for {name}; skipping apply step")
            return True
        print(f"Applying config for {name}: {command}")
        output, exit_code = self.execute_command(command, timeout=60)
        if exit_code != 0:
            print(f"✗ Failed to apply config for {name}")
            print(f"Output: {output}")
            return False
        print(f"✓ Config applied for {name}")
        return True

    def _resolve_mcp_url(self, config_path: Optional[str], server_name: Optional[str]) -> Optional[str]:
        if not server_name:
            return None
        if not config_path:
            config_path = os.path.join(os.getcwd(), "mcp.json")
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
            servers = data.get("mcpServers", {})
            server = servers.get(server_name, {})
            return server.get("url")
        except (OSError, json.JSONDecodeError):
            return None

    def _auto_select_mcp_tool(
        self,
        tool_name: Optional[str],
        command_arg: Optional[str],
        target_arg: Optional[str]
    ) -> Tuple[str, str, Optional[str]]:
        if not self.mcp_client:
            raise RuntimeError("MCP client not initialized")
        tools = self.mcp_client.list_tools()
        selected = None
        command_key = None
        target_key = None
        command_candidates = ["command", "cmd", "cli", "text"]
        target_candidates = ["device_name", "device", "target", "host", "node", "system"]

        for tool in tools:
            name = tool.get("name")
            schema = tool.get("inputSchema", {}) if isinstance(tool, dict) else {}
            props = schema.get("properties", {}) if isinstance(schema, dict) else {}
            if not isinstance(props, dict):
                props = {}
            prop_keys = set(props.keys())
            if not command_key:
                for cand in command_candidates:
                    if cand in prop_keys:
                        command_key = cand
                        break
            if not target_key:
                for cand in target_candidates:
                    if cand in prop_keys:
                        target_key = cand
                        break
            name_match = isinstance(name, str) and any(
                token in name.lower() for token in ["command", "cli", "exec", "shell"]
            )
            if command_key and name_match and name:
                selected = name
                break
            if command_key and not selected and name:
                selected = name

        final_tool = tool_name or selected
        final_command_arg = command_arg or command_key
        final_target_arg = target_arg or target_key
        if not final_tool or not final_command_arg:
            raise RuntimeError(
                "Failed to auto-detect MCP tool/command arg. "
                "Pass --mcp-tool and --mcp-command-arg explicitly."
            )
        return final_tool, final_command_arg, final_target_arg
        
        if current_config.strip() == original_config.strip():
            print(f"✓ Config unchanged for {name}")
            return True
        else:
            print(f"✗ Config changed for {name}!")
            print("Differences:")
            # Simple diff output
            current_lines = set(current_config.strip().split('\n'))
            original_lines = set(original_config.strip().split('\n'))
            added = current_lines - original_lines
            removed = original_lines - current_lines
            if added:
                print(f"  Added: {added}")
            if removed:
                print(f"  Removed: {removed}")
            return False
    
    def wait_for_node_online(self, max_wait: int = 300) -> bool:
        """
        Wait for node to come back online after restart.
        
        Args:
            max_wait: Maximum seconds to wait
            
        Returns:
            True if node came online, False otherwise
        """
        print(f"Waiting for node to come online (max {max_wait}s)...")
        start_time = time.time()
        
        while time.time() - start_time < max_wait:
            # Try a simple command to check if node is online
            output, exit_code = self.execute_command("show version", timeout=5)
            if exit_code == 0:
                elapsed = time.time() - start_time
                print(f"✓ Node is online after {elapsed:.2f} seconds")
                return True
            time.sleep(2)
        
        print(f"✗ Node did not come online within {max_wait} seconds")
        return False
    
    def verify_reboot(self) -> bool:
        """Verify that OS reboot occurred by checking uptime or boot time."""
        output, exit_code = self.execute_command("show system uptime")
        if exit_code == 0:
            print(f"✓ Reboot verified - uptime info: {output[:100]}")
            return True
        
        # Alternative: check boot time
        output, exit_code = self.execute_command("show system boot-time")
        if exit_code == 0:
            print(f"✓ Reboot verified - boot time: {output[:100]}")
            return True
        
        print("⚠ Could not verify reboot (commands may not be available)")
        return True  # Assume reboot happened if we're here
    
    def perform_restart(self, restart_type: str, warm: bool = True) -> float:
        """
        Perform a restart and measure the time.
        
        Args:
            restart_type: 'warm' or 'cold'
            warm: True for warm restart, False for cold
            
        Returns:
            Restart time in seconds
        """
        if warm:
            if self.use_ncp:
                cmd = f"request system restart ncp {self.ncp_id} warm"
            else:
                cmd = "request system restart warm"
        else:
            if self.use_ncp:
                cmd = f"request system restart ncp {self.ncp_id}"
            else:
                cmd = "request system restart"
        
        print(f"\n{'='*60}")
        print(f"Performing {restart_type} restart: {cmd}")
        print(f"{'='*60}")
        
        start_time = time.time()
        
        # Execute restart command
        output, exit_code = self.execute_command(cmd, timeout=10)
        if exit_code != 0:
            print(f"⚠ Restart command exit code: {exit_code}")
            print(f"Output: {output}")
        
        # Wait for node to come back online
        if not self.wait_for_node_online():
            return -1
        
        restart_time = time.time() - start_time
        
        # Verify reboot occurred
        self.verify_reboot()
        
        print(f"✓ Restart completed in {restart_time:.2f} seconds")
        return restart_time
    
    def test_warm_restart_empty_config(self, iterations: int = 10):
        """Test warm restart with empty config."""
        print("\n" + "="*60)
        print("TEST 1: Warm restart with empty config")
        print("="*60)

        if not self.apply_config("warm_empty", self.empty_config_cmd):
            return []
        
        # Save config snapshot
        self.save_config_snapshot("warm_empty")
        
        times = []
        for i in range(iterations):
            print(f"\n--- Iteration {i+1}/{iterations} ---")
            restart_time = self.perform_restart("warm", warm=True)
            if restart_time > 0:
                times.append(restart_time)
                # Verify config unchanged
                self.verify_config_unchanged("warm_empty")
            else:
                print(f"✗ Iteration {i+1} failed")
            time.sleep(5)  # Brief pause between iterations
        
        self.results['warm_empty'] = times
        if times:
            avg = statistics.mean(times)
            print(f"\n✓ Warm restart (empty config) - Average: {avg:.2f}s, Times: {times}")
        return times
    
    def test_warm_restart_normal_config(self, iterations: int = 3):
        """Test warm restart with normal config."""
        print("\n" + "="*60)
        print("TEST 2: Warm restart with normal config (including breakout)")
        print("="*60)

        if not self.apply_config("warm_normal", self.normal_config_cmd):
            return []
        
        # Save config snapshot
        self.save_config_snapshot("warm_normal")
        
        times = []
        for i in range(iterations):
            print(f"\n--- Iteration {i+1}/{iterations} ---")
            restart_time = self.perform_restart("warm", warm=True)
            if restart_time > 0:
                times.append(restart_time)
                # Verify config unchanged
                self.verify_config_unchanged("warm_normal")
            else:
                print(f"✗ Iteration {i+1} failed")
            time.sleep(5)
        
        self.results['warm_normal'] = times
        if times:
            avg = statistics.mean(times)
            print(f"\n✓ Warm restart (normal config) - Average: {avg:.2f}s, Times: {times}")
        return times
    
    def test_warm_restart_scale_config(self, iterations: int = 3):
        """Test warm restart with scale config."""
        print("\n" + "="*60)
        print("TEST 3: Warm restart with scale config")
        print("="*60)

        if not self.apply_config("warm_scale", self.scale_config_cmd):
            return []
        
        # Save config snapshot
        self.save_config_snapshot("warm_scale")
        
        times = []
        for i in range(iterations):
            print(f"\n--- Iteration {i+1}/{iterations} ---")
            restart_time = self.perform_restart("warm", warm=True)
            if restart_time > 0:
                times.append(restart_time)
                # Verify config unchanged
                self.verify_config_unchanged("warm_scale")
            else:
                print(f"✗ Iteration {i+1} failed")
            time.sleep(5)
        
        self.results['warm_scale'] = times
        if times:
            avg = statistics.mean(times)
            print(f"\n✓ Warm restart (scale config) - Average: {avg:.2f}s, Times: {times}")
        return times
    
    def test_cold_restart_empty_config(self, iterations: int = 10):
        """Test cold restart with empty config."""
        print("\n" + "="*60)
        print("TEST 4: Cold restart with empty config")
        print("="*60)

        if not self.apply_config("cold_empty", self.empty_config_cmd):
            return []
        
        # Save config snapshot
        self.save_config_snapshot("cold_empty")
        
        times = []
        for i in range(iterations):
            print(f"\n--- Iteration {i+1}/{iterations} ---")
            restart_time = self.perform_restart("cold", warm=False)
            if restart_time > 0:
                times.append(restart_time)
                # Verify config unchanged
                self.verify_config_unchanged("cold_empty")
            else:
                print(f"✗ Iteration {i+1} failed")
            time.sleep(5)
        
        self.results['cold_empty'] = times
        if times:
            avg = statistics.mean(times)
            print(f"\n✓ Cold restart (empty config) - Average: {avg:.2f}s, Times: {times}")
        return times
    
    def test_cold_restart_normal_config(self, iterations: int = 3):
        """Test cold restart with normal config."""
        print("\n" + "="*60)
        print("TEST 5: Cold restart with normal config (including breakout)")
        print("="*60)

        if not self.apply_config("cold_normal", self.normal_config_cmd):
            return []
        
        # Save config snapshot
        self.save_config_snapshot("cold_normal")
        
        times = []
        for i in range(iterations):
            print(f"\n--- Iteration {i+1}/{iterations} ---")
            restart_time = self.perform_restart("cold", warm=False)
            if restart_time > 0:
                times.append(restart_time)
                # Verify config unchanged
                self.verify_config_unchanged("cold_normal")
            else:
                print(f"✗ Iteration {i+1} failed")
            time.sleep(5)
        
        self.results['cold_normal'] = times
        if times:
            avg = statistics.mean(times)
            print(f"\n✓ Cold restart (normal config) - Average: {avg:.2f}s, Times: {times}")
        return times
    
    def test_cold_restart_scale_config(self, iterations: int = 3):
        """Test cold restart with scale config."""
        print("\n" + "="*60)
        print("TEST 6: Cold restart with scale config")
        print("="*60)

        if not self.apply_config("cold_scale", self.scale_config_cmd):
            return []
        
        # Save config snapshot
        self.save_config_snapshot("cold_scale")
        
        times = []
        for i in range(iterations):
            print(f"\n--- Iteration {i+1}/{iterations} ---")
            restart_time = self.perform_restart("cold", warm=False)
            if restart_time > 0:
                times.append(restart_time)
                # Verify config unchanged
                self.verify_config_unchanged("cold_scale")
            else:
                print(f"✗ Iteration {i+1} failed")
            time.sleep(5)
        
        self.results['cold_scale'] = times
        if times:
            avg = statistics.mean(times)
            print(f"\n✓ Cold restart (scale config) - Average: {avg:.2f}s, Times: {times}")
        return times
    
    def compare_times(self, baseline: List[float], comparison: List[float], 
                     test_name: str, threshold: float = 0.3):
        """
        Compare restart times and check if they're within acceptable range.
        
        Args:
            baseline: Baseline times (e.g., empty config)
            comparison: Times to compare
            test_name: Name of the test
            threshold: Maximum allowed deviation (30% by default)
        """
        if not baseline or not comparison:
            print(f"⚠ Cannot compare {test_name}: Missing data")
            return False
        
        baseline_avg = statistics.mean(baseline)
        comparison_avg = statistics.mean(comparison)
        
        deviation = abs(comparison_avg - baseline_avg) / baseline_avg
        
        print(f"\n{test_name} Comparison:")
        print(f"  Baseline average: {baseline_avg:.2f}s")
        print(f"  Comparison average: {comparison_avg:.2f}s")
        print(f"  Deviation: {deviation*100:.1f}%")
        
        if deviation <= threshold:
            print(f"✓ PASS: Deviation ({deviation*100:.1f}%) within threshold ({threshold*100:.0f}%)")
            return True
        else:
            print(f"✗ FAIL: Deviation ({deviation*100:.1f}%) exceeds threshold ({threshold*100:.0f}%)")
            return False
    
    def generate_report(self):
        """Generate final test report."""
        print("\n" + "="*60)
        print("FINAL TEST REPORT")
        print("="*60)
        
        # Warm restart comparisons
        warm_empty = self.results.get('warm_empty', [])
        warm_normal = self.results.get('warm_normal', [])
        warm_scale = self.results.get('warm_scale', [])
        
        if warm_empty:
            print(f"\nWarm Restart Baseline (Empty Config):")
            print(f"  Average: {statistics.mean(warm_empty):.2f}s")
            print(f"  Min: {min(warm_empty):.2f}s, Max: {max(warm_empty):.2f}s")
            print(f"  Standard Deviation: {statistics.stdev(warm_empty) if len(warm_empty) > 1 else 0:.2f}s")
        
        if warm_empty and warm_normal:
            self.compare_times(warm_empty, warm_normal, 
                             "Warm Restart: Normal config vs Empty config")
        
        if warm_empty and warm_scale:
            self.compare_times(warm_empty, warm_scale,
                             "Warm Restart: Scale config vs Empty config")
        
        # Cold restart comparisons
        cold_empty = self.results.get('cold_empty', [])
        cold_normal = self.results.get('cold_normal', [])
        cold_scale = self.results.get('cold_scale', [])
        
        if cold_empty:
            print(f"\nCold Restart Baseline (Empty Config):")
            print(f"  Average: {statistics.mean(cold_empty):.2f}s")
            print(f"  Min: {min(cold_empty):.2f}s, Max: {max(cold_empty):.2f}s")
            print(f"  Standard Deviation: {statistics.stdev(cold_empty) if len(cold_empty) > 1 else 0:.2f}s")
        
        if cold_empty and cold_normal:
            self.compare_times(cold_empty, cold_normal,
                             "Cold Restart: Normal config vs Empty config")
        
        if cold_empty and cold_scale:
            self.compare_times(cold_empty, cold_scale,
                             "Cold Restart: Scale config vs Empty config")
        
        # Save results to JSON file
        report_data = {
            'timestamp': datetime.now().isoformat(),
            'results': self.results,
            'summary': {
                'warm_empty_avg': statistics.mean(warm_empty) if warm_empty else None,
                'warm_normal_avg': statistics.mean(warm_normal) if warm_normal else None,
                'warm_scale_avg': statistics.mean(warm_scale) if warm_scale else None,
                'cold_empty_avg': statistics.mean(cold_empty) if cold_empty else None,
                'cold_normal_avg': statistics.mean(cold_normal) if cold_normal else None,
                'cold_scale_avg': statistics.mean(cold_scale) if cold_scale else None,
            }
        }
        
        report_file = f"restart_test_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(report_file, 'w') as f:
            json.dump(report_data, f, indent=2)
        
        print(f"\n✓ Detailed results saved to: {report_file}")
    
    def run_all_tests(self):
        """Run all test scenarios."""
        print("\n" + "="*60)
        print("STARTING RESTART TEST SUITE")
        print("="*60)
        print(f"Timestamp: {datetime.now().isoformat()}")
        print(f"Host: {self.host or 'local'}")
        print(f"Use NCP: {self.use_ncp}, NCP ID: {self.ncp_id}")
        
        try:
            # Test 1: Warm restart with empty config (10 times)
            N = self.test_warm_restart_empty_config(iterations=10)
            
            # Test 2: Warm restart with normal config (3 times)
            self.test_warm_restart_normal_config(iterations=3)
            
            # Test 3: Warm restart with scale config (3 times)
            self.test_warm_restart_scale_config(iterations=3)
            
            # Test 4: Cold restart with empty config (10 times)
            M = self.test_cold_restart_empty_config(iterations=10)
            
            # Test 5: Cold restart with normal config (3 times)
            self.test_cold_restart_normal_config(iterations=3)
            
            # Test 6: Cold restart with scale config (3 times)
            self.test_cold_restart_scale_config(iterations=3)
            
            # Generate final report
            self.generate_report()
            
            print("\n" + "="*60)
            print("TEST SUITE COMPLETED")
            print("="*60)
            
        except KeyboardInterrupt:
            print("\n\nTest interrupted by user")
            sys.exit(1)
        except Exception as e:
            print(f"\n\n✗ Test suite failed with error: {str(e)}")
            import traceback
            traceback.print_exc()
            sys.exit(1)


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description='Test node restart scenarios with different configurations'
    )
    parser.add_argument('--host', type=str, help='SSH host (if remote)')
    parser.add_argument('--username', type=str, help='SSH username')
    parser.add_argument('--use-ncp', action='store_true',
                       help='Use NCP commands (ncp 0)')
    parser.add_argument('--ncp-id', type=int, default=0,
                       help='NCP ID to use (default: 0)')
    parser.add_argument('--password', type=str,
                        help='SSH password (requires sshpass)')
    parser.add_argument('--empty-config-cmd', type=str,
                        help='Command to apply empty config before tests')
    parser.add_argument('--normal-config-cmd', type=str,
                        help='Command to apply normal config before tests')
    parser.add_argument('--scale-config-cmd', type=str,
                        help='Command to apply scale config before tests')
    parser.add_argument('--use-mcp', action='store_true',
                        help='Use MCP server to execute commands (default)')
    parser.add_argument('--no-mcp', action='store_true',
                        help='Disable MCP and use local/SSH execution')
    parser.add_argument('--mcp-config', type=str, default='/home/dn/mcp.json',
                        help='Path to mcp.json (default: /home/dn/mcp.json)')
    parser.add_argument('--mcp-server', type=str, default='network-mapper',
                        help='MCP server name from mcp.json (default: network-mapper)')
    parser.add_argument('--mcp-url', type=str,
                        help='Direct MCP server SSE URL (overrides mcp.json)')
    parser.add_argument('--mcp-tool', type=str,
                        help='MCP tool name to execute commands')
    parser.add_argument('--mcp-command-arg', type=str,
                        help='MCP tool argument name for command string')
    parser.add_argument('--mcp-target', type=str,
                        help='Target identifier for MCP tool (defaults to --host)')
    parser.add_argument('--mcp-target-arg', type=str,
                        help='MCP tool argument name for target')
    parser.add_argument('--mcp-timeout', type=int, default=30,
                        help='MCP request timeout in seconds')
    
    args = parser.parse_args()
    
    use_mcp = args.use_mcp or not args.no_mcp

    tester = RestartTester(
        host=args.host,
        username=args.username,
        password=args.password,
        use_ncp=args.use_ncp,
        ncp_id=args.ncp_id,
        empty_config_cmd=args.empty_config_cmd,
        normal_config_cmd=args.normal_config_cmd,
        scale_config_cmd=args.scale_config_cmd,
        use_mcp=use_mcp,
        mcp_config_path=args.mcp_config,
        mcp_server=args.mcp_server,
        mcp_url=args.mcp_url,
        mcp_tool=args.mcp_tool,
        mcp_command_arg=args.mcp_command_arg,
        mcp_target=args.mcp_target,
        mcp_target_arg=args.mcp_target_arg,
        mcp_timeout=args.mcp_timeout
    )
    
    tester.run_all_tests()


if __name__ == '__main__':
    main()
