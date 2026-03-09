# SW-235385 - Ethernet OAM Y.1731 SNMP Test

Jira: https://drivenets.atlassian.net/browse/SW-235385

## Objective
Validate DRIVENETS-CFM-MIB SNMP coverage for on-demand and proactive Y.1731
sessions (DM/SLM), including session info, results tables, and proactive
failure trap fields.

## Preconditions
- DUT reachable via SNMP (mgmt0 or relevant VRF).
- SNMP community configured on DUT.
- At least one MD/MA/MEP configured and operational.
- Ability to run on-demand DM/SLM and configure proactive DM/SLM sessions.
- MIBs loaded in SNMP browser or available for snmpwalk.

## Test Data
- MD name: <MD_NAME>
- MA name: <MA_NAME>
- Source MEP ID: <MEP_ID>
- Target MEP ID or MAC: <TARGET>
- Interface: <IF_NAME>
- PCP: <PCP_VALUE>
- Count/Interval/Timeout: <COUNT>/<INTERVAL>/<TIMEOUT>

## Steps
1) Configure SNMP on DUT.
   - Verify `snmpwalk` works for base system OIDs.

2) Run on-demand DM (two-way) with valid target.
   - Record start/end times and expected counts.

3) Run on-demand SLM (synthetic loss) with valid target.
   - Record start/end times and expected counts.

4) Configure proactive DM session.
   - Enable and allow it to run at least one computation interval.

5) Configure proactive SLM session.
   - Enable and allow it to run at least one computation interval.

6) SNMP walk the following tables and verify values:
   - `dnCfmOnDemandDmTestInfoTable`
   - `dnCfmOnDemandDmTestResultsTable`
   - `dnCfmOnDemandSlmTestInfoTable`
   - `dnCfmOnDemandSlmTestResultsTable`
   - `dnCfmProactiveDmSessionTable`
   - `dnCfmProactiveDmResultTable`
   - `dnCfmProactiveSlmSessionTable`
   - `dnCfmProactiveSlmResultTable`

7) Verify each table contains:
   - Correct MD/MA/MEP identifiers.
   - Correct target type (MEP ID vs MAC) and target values.
   - Correct source interface/MAC.
   - Correct PCP/count/interval/timeout values.
   - Results: validity, transmitted/received counts, success rate,
     delay/jitter or loss counters as applicable.

8) Trigger a proactive threshold violation (if supported by profile).
   - Confirm SNMP trap `dnCfmProactiveTestFailure` is received.
   - Validate trap fields: source MD/MA/MEP and session ID.

## Expected Results
- All SNMP tables are populated for DM/SLM on-demand and proactive sessions.
- Table entries match CLI config and show outputs.
- Result counters and rates are consistent with observed test behavior.
- Trap is emitted on proactive failure with correct fields.

## Pass/Fail Criteria
- Pass if all relevant tables are populated and values match CLI/oper data,
  and the proactive failure trap is correctly emitted.
- Fail if any table is missing, values are incorrect, or trap is missing.
