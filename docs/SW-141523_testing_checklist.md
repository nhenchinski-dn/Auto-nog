CLI -
- [ ] SW-235373 | performance-monitoring | cfm | two-way-delay-measurement
- [ ] SW-235927 | performance-monitoring | cfm | two-way-synthetic-loss-measurement
- [ ] SW-235375 | performance-monitoring | Profiles | two-way-delay-measurement
- [ ] SW-236444 | performance-monitoring | Profiles | two-way-synthetic-loss-measurement
- [ ] SW-236451 | Profiles | two-way-synthetic-loss-measurement | test duration
- [ ] SW-236465 | Profiles | two-way-delay-measurement | test duration
- [ ] SW-236452 | Profiles | two-way-delay-measurement | thresholds
- [ ] SW-236457 | Profiles | two-way-synthetic-loss-measurement | thresholds
- [ ] SW-235376 | Show Commands
- [ ] SW-237984 | request ethernet-oam cfm on-demand stop

Functionality -
- [ ] SW-235378 | Functionality
  - [ ] SW-236664 | PM | ETH-DM Initiator
  - [ ] SW-236665 | PM | ETH-SLM Initiator
  - [ ] SW-236666 | PM | ETH-LM Initiator
  - [ ] SW-236667 | PM | Session Establishment Modes (With/Without CCM)
  - [ ] SW-236668 | PM | Session Timing Range
  - [ ] SW-236669 | PM | VLAN Tagging and L2
  - [ ] SW-236670 | FM | ETH-AIS Proactive Alarm Suppression
  - [ ] SW-236671 | FM | ETH-LB Initiator (Unicast + Multicast)
  - [ ] SW-237053 | FM | System Event

Scale -
- [ ] SW-235380 | Scale
  - [ ] SW-236988 | Sessions per MA
  - [ ] SW-236989 | Initiator Limits + Per-MEP + System
  - [ ] SW-236991 | Responder Sessions per System + MA

HA -
- [ ] SW-235379 | HA
  - [ ] SW-237045 | HA

SNMP -
- [ ] SW-235385 | SNMP
  - [ ] SW-237080 | SNMP

NETCONF / GNMI / RESTConf -
- [ ] SW-235381 | NETCONF, GNMI & RESTConf
  - [ ] SW-237066 | NETCONF
  - [ ] SW-237067 | RESTCONF
  - [ ] SW-237068 | GNMI

Statistics / Buckets / Thresholds -
- [ ] SW-235386 | Statistics, Buckets & Thresholds
  - [ ] SW-238001 | DM Statistics (proactive) update + “buckets” presence
  - [ ] SW-238003 | SLM Statistics (proactive) update + “buckets” presence
  - [ ] SW-238005 | DM profile show output
  - [ ] SW-238031 | SLM profile show output
  - [ ] SW-238006 | Inform-test-results toggle effect (DM + SLM)
  - [ ] SW-238007 | Clear/reset behavior

Testing Guidance (from epic) -
- [ ] Test on single-tagged VLANs
- [ ] Test on double-tagged VLANs
- [ ] 2000 MEPs, each with 1 LMM/DMM/SLM session
- [ ] 20 MEPs, each with 100 LMM/DMM/SLM sessions
- [ ] Multiple LMM/DMM/SLM sessions on the same MEP, each with a different PCP value
- [ ] LMM/DMM/SLM sessions with CCM messages either enabled or disabled
- [ ] Work with specific L2 <s,c> and VLAN list sub-interfaces

Focus Areas (from epic comments) -
- [ ] CLI tests (a lot of CLI here)
- [ ] Scale testing
- [ ] SNMP (can be done at the end, probably not much testing)

No need to focus on (from epic comments) -
- [ ] interop
- [ ] different kinds of interfaces / services / ...
