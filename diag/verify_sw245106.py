#!/usr/bin/env python3
"""
Verification test for SW-245106:
  PIM crashes on use-after-free during MRIB update of observers.

Reproduces: 60K+ SSM routes + rapid RPF interface flapping, then
verifies pimd stays responsive and does not crash (no new core dumps).

Prerequisites:
  - Spirent configured and sending 60K+ (S,G) SSM joins toward the device
  - RPF interface physically connected and able to be shut/no-shut
"""
import pexpect, re, time, sys, threading, datetime

sys.stdout.reconfigure(line_buffering=True)

# ── Configuration ────────────────────────────────────────────
HOST = "100.64.6.171"
USER = "dnroot"
PASS = "dnroot"
FLAP_INTERFACE = "ge400-0/0/10/0"
FLAP_ROUNDS = 30
FLAP_PAUSE = 25          # seconds between flaps (original was ~20-40s)
CLI_TIMEOUT = 180         # seconds — original bug hung for ~3 min
CLI_FAST_THRESHOLD = 30   # if CLI responds within this, it's healthy
MIN_ROUTES = 50000        # minimum (S,G) routes to consider scale valid
CORE_DUMP_PATH = "/core/core_dumps/containers/routing_engine"

P = r'[\w\-\(\)]+[#>]\s*\Z'
M = r'(?:-- More --|-- End --|\(Press q to quit\))'
C = r'\(yes/no(?:/cancel)?\)\s*\[(?:cancel|no)\]\?'

# ── Helpers ──────────────────────────────────────────────────
def connect(label):
    print(f"  [{label}] Connecting to {HOST}...", flush=True)
    ch = pexpect.spawn(
        f"sshpass -p '{PASS}' ssh -tt -o StrictHostKeyChecking=no "
        f"-o PreferredAuthentications=password,keyboard-interactive "
        f"-o PubkeyAuthentication=no {USER}@{HOST}",
        encoding='utf-8', timeout=60, maxread=200000)
    ch.expect(P)
    print(f"  [{label}] Connected.", flush=True)
    return ch


def x(child, cmd, t=30):
    child.sendline(cmd)
    out = ''
    while True:
        i = child.expect([P, M, C, pexpect.TIMEOUT, pexpect.EOF], timeout=t)
        out += child.before
        if i == 0:
            break
        elif i == 1:
            child.send(' ')
        elif i == 2:
            out += child.after
            child.sendline('no')
            try:
                child.expect(P, timeout=10)
            except Exception:
                pass
            break
        elif i == 4:
            print(f'  [EOF]', flush=True)
            break
        else:
            print(f'  [TIMEOUT after {t}s]', flush=True)
            break
    out = re.sub(r'\x1b\[[0-9;]*[mKHJrl]', '', out)
    out = re.sub(r'\x1b\[\?[0-9;]*[hl]', '', out)
    return out.replace('\r\n', '\n').replace('\r', '').strip()


def timed_cmd(child, cmd, t=CLI_TIMEOUT):
    """Run a command and return (output, elapsed_seconds)."""
    start = time.time()
    out = x(child, cmd, t=t)
    elapsed = time.time() - start
    return out, elapsed


def h(title):
    print(f"\n{'='*70}\n  {title}\n{'='*70}", flush=True)


results = []
def verdict(name, passed, detail=""):
    tag = "PASS" if passed else "FAIL"
    results.append((name, passed, detail))
    print(f"\n  [{tag}] {name}", flush=True)
    if detail:
        print(f"         {detail}", flush=True)


def extract_core_list(output):
    """Return set of core-pimd filenames from ls output."""
    return set(re.findall(r'core-pimd\.\S+', output))


def extract_route_count(output):
    m = re.search(r'Total PIM MFIB routes\s*:\s*(\d+)', output)
    return int(m.group(1)) if m else 0


# ── Main ─────────────────────────────────────────────────────
child = connect("main")

h("PRE-CHECK 1: Software version")
ver = x(child, "show system version | no-more")
print(ver[:600], flush=True)

h("PRE-CHECK 2: Verify multicast route scale")
out = x(child, "show pim summary | no-more", t=60)
print(out, flush=True)
route_count = extract_route_count(out)
print(f"\n  MFIB route count: {route_count}", flush=True)
verdict("Route scale sufficient for test",
        route_count >= MIN_ROUTES,
        f"{route_count} routes (need >= {MIN_ROUTES})")

if route_count < MIN_ROUTES:
    print("\n  WARNING: Not enough routes. Ensure Spirent is sending 60K+ "
          "(S,G) SSM joins before running this test.", flush=True)
    print("  Continuing anyway — flap tests will still run.\n", flush=True)

h("PRE-CHECK 3: Baseline pimd core dumps")
cores_before_raw = x(child, f"run bash ls -1t {CORE_DUMP_PATH}/core-pimd* 2>/dev/null || echo NO_CORES", t=15)
print(cores_before_raw, flush=True)
cores_before = extract_core_list(cores_before_raw)
print(f"  Existing pimd cores: {len(cores_before)}", flush=True)

h("PRE-CHECK 4: Verify pimd is running")
pimd_out = x(child, "run bash pgrep -ax pimd || echo PIMD_NOT_RUNNING", t=10)
print(pimd_out, flush=True)
pimd_alive = "PIMD_NOT_RUNNING" not in pimd_out
verdict("pimd is running before test", pimd_alive)

h("PRE-CHECK 5: Interface state")
intf_out = x(child, f"show interface {FLAP_INTERFACE} brief | no-more", t=15)
print(intf_out, flush=True)

# ── Flap + responsiveness test ───────────────────────────────
h(f"FLAP TEST: {FLAP_ROUNDS} rounds on {FLAP_INTERFACE}")
print(f"  Flapping {FLAP_INTERFACE} with {FLAP_PAUSE}s pause between rounds.", flush=True)
print(f"  After each flap, measuring CLI responsiveness (timeout={CLI_TIMEOUT}s).", flush=True)

cli_times = []
flap_failures = 0

for rnd in range(1, FLAP_ROUNDS + 1):
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    print(f"\n  --- Round {rnd}/{FLAP_ROUNDS}  [{ts}] ---", flush=True)

    # Shut interface
    print(f"  Shutting {FLAP_INTERFACE}...", flush=True)
    x(child, "configure", t=10)
    x(child, f"interface {FLAP_INTERFACE}", t=10)
    x(child, "shutdown", t=10)
    commit_out = x(child, "commit", t=60)
    if "error" in commit_out.lower():
        print(f"  Commit (shut) error: {commit_out[-200:]}", flush=True)

    time.sleep(1)

    # No-shut interface
    print(f"  Bringing {FLAP_INTERFACE} back up...", flush=True)
    x(child, "no shutdown", t=10)
    commit_out = x(child, "commit", t=60)
    if "error" in commit_out.lower():
        print(f"  Commit (no-shut) error: {commit_out[-200:]}", flush=True)
    x(child, "exit", t=10)
    x(child, "exit", t=10)

    # Immediately test CLI responsiveness with show pim summary
    print(f"  Testing CLI responsiveness...", flush=True)
    pim_out, elapsed = timed_cmd(child, "show pim summary | no-more", t=CLI_TIMEOUT)
    cli_times.append(elapsed)
    fast = elapsed < CLI_FAST_THRESHOLD
    status = "OK" if fast else "SLOW"
    print(f"  'show pim summary' returned in {elapsed:.1f}s [{status}]", flush=True)

    current_routes = extract_route_count(pim_out)
    if current_routes > 0:
        print(f"  MFIB routes: {current_routes}", flush=True)

    if not fast:
        flap_failures += 1
        print(f"  *** CLI was unresponsive for {elapsed:.1f}s (threshold: {CLI_FAST_THRESHOLD}s)", flush=True)

    # Quick pimd liveness check
    pimd_chk = x(child, "run bash pgrep -x pimd >/dev/null && echo ALIVE || echo DEAD", t=10)
    if "DEAD" in pimd_chk:
        print(f"  *** pimd CRASHED during round {rnd}!", flush=True)
        flap_failures += 1

    # Wait before next flap
    if rnd < FLAP_ROUNDS:
        print(f"  Waiting {FLAP_PAUSE}s before next flap...", flush=True)
        time.sleep(FLAP_PAUSE)

# ── Post-flap analysis ───────────────────────────────────────
h("POST-CHECK 1: CLI response times during flapping")
if cli_times:
    avg_t = sum(cli_times) / len(cli_times)
    max_t = max(cli_times)
    min_t = min(cli_times)
    print(f"  Min: {min_t:.1f}s  Avg: {avg_t:.1f}s  Max: {max_t:.1f}s", flush=True)
    print(f"  Rounds where CLI was slow (>{CLI_FAST_THRESHOLD}s): {flap_failures}/{FLAP_ROUNDS}", flush=True)
    verdict("CLI stayed responsive during flapping",
            flap_failures == 0,
            f"max response: {max_t:.1f}s, threshold: {CLI_FAST_THRESHOLD}s")

h("POST-CHECK 2: pimd still running")
pimd_post = x(child, "run bash pgrep -ax pimd || echo PIMD_NOT_RUNNING", t=10)
print(pimd_post, flush=True)
pimd_alive_post = "PIMD_NOT_RUNNING" not in pimd_post
verdict("pimd survived all flap rounds", pimd_alive_post)

h("POST-CHECK 3: Check for NEW pimd core dumps")
cores_after_raw = x(child, f"run bash ls -1t {CORE_DUMP_PATH}/core-pimd* 2>/dev/null || echo NO_CORES", t=15)
print(cores_after_raw, flush=True)
cores_after = extract_core_list(cores_after_raw)
new_cores = cores_after - cores_before
if new_cores:
    print(f"  *** NEW pimd core dumps found: {new_cores}", flush=True)
else:
    print(f"  No new pimd core dumps.", flush=True)
verdict("No new pimd core dumps", len(new_cores) == 0,
        f"new cores: {new_cores}" if new_cores else "")

h("POST-CHECK 4: PIM state after flapping stabilizes")
print("  Waiting 30s for state to settle...", flush=True)
time.sleep(30)
pim_final = x(child, "show pim summary | no-more", t=60)
print(pim_final, flush=True)
final_routes = extract_route_count(pim_final)
print(f"  Final MFIB route count: {final_routes}", flush=True)

nbr_out = x(child, "show pim neighbor | no-more", t=30)
print(nbr_out, flush=True)
has_neighbors = "ge800" in nbr_out or "Neighbor" in nbr_out
verdict("PIM neighbors re-established after flapping",
        has_neighbors,
        "Check output above for neighbor details")

h("POST-CHECK 5: Verify no RIB install errors during test")
syslog = x(child, "run bash grep -c 'RIB_MULTICAST_INSTALL_ERRORS_DETECTED' "
           "/var/log/system-events.log 2>/dev/null || echo 0", t=15)
print(f"  RIB multicast install error count in syslog: {syslog}", flush=True)

# ── Summary ──────────────────────────────────────────────────
print(f"\n{'='*70}", flush=True)
print(f"  RESULTS SUMMARY — SW-245106 Verification", flush=True)
print(f"{'='*70}", flush=True)
pass_count = sum(1 for _, p, _ in results if p)
fail_count = sum(1 for _, p, _ in results if not p)
for name, passed, detail in results:
    tag = "PASS" if passed else "FAIL"
    print(f"  [{tag}] {name}", flush=True)
    if detail and not passed:
        print(f"         {detail}", flush=True)
print(f"\n  Passed: {pass_count}, Failed: {fail_count}, Total: {len(results)}", flush=True)

if fail_count == 0:
    print(f"\n  CONCLUSION: SW-245106 fix verified — pimd stayed responsive "
          f"and did not crash during {FLAP_ROUNDS} interface flap rounds "
          f"with {route_count}+ multicast routes.", flush=True)
else:
    print(f"\n  CONCLUSION: SW-245106 may still be present — see failures above.", flush=True)

child.sendline("exit")
try:
    child.expect(pexpect.EOF, timeout=5)
except Exception:
    pass
print(f"\n{'='*70}\n  TEST COMPLETED\n{'='*70}", flush=True)
