#!/usr/bin/env python3
"""
SNMP Trap Listener for SW-239712 testing.
Listens on UDP port 9162 (non-privileged) and logs all received traps
with full OID resolution using the local DRIVENETS-CFM-MIB.
"""

import socket
import struct
import datetime
import sys
import signal
import json

LISTEN_PORT = 9162
TRAP_LOG = "/home/dn/output/trap_log.json"

CFM_OID_PREFIX = "1.3.6.1.4.1.49739.2.15"

OID_NAMES = {
    f"{CFM_OID_PREFIX}.0.1": "dnCfmFaultAlarm",
    f"{CFM_OID_PREFIX}.0.2": "dnCfmFaultAlarmCleared",
    f"{CFM_OID_PREFIX}.0.3": "dnCfmProactiveTestFailure",
    f"{CFM_OID_PREFIX}.1.5.0": "dnCfmTrapMdLevel",
    f"{CFM_OID_PREFIX}.1.6.0": "dnCfmTrapMdName",
    f"{CFM_OID_PREFIX}.1.7.0": "dnCfmTrapMaName",
    f"{CFM_OID_PREFIX}.1.8.0": "dnCfmTrapHighestDefectPri",
    f"{CFM_OID_PREFIX}.1.9.0": "dnCfmTrapThresholdType",
    f"{CFM_OID_PREFIX}.1.10.0": "dnCfmTrapThresholdValue",
    f"{CFM_OID_PREFIX}.1.11.0": "dnCfmTrapMeasuredValue",
    f"{CFM_OID_PREFIX}.1.12.0": "dnCfmTrapMepId",
    f"{CFM_OID_PREFIX}.1.13.0": "dnCfmTrapSessionType",
    f"{CFM_OID_PREFIX}.1.14.0": "dnCfmTrapSessionId",
}

DEFECT_PRI = {0: "none", 1: "defRDICCM", 2: "defMACstatus", 3: "defRemoteCCM", 4: "defErrorCCM", 5: "defXconCCM"}
THRESHOLD_TYPE = {1: "frameDelayTwoWayMin", 2: "frameDelayTwoWayAvg", 3: "frameDelayTwoWayMax",
                  4: "ifdvTwoWayAvg", 5: "ifdvTwoWayMax", 6: "successRatePercent",
                  7: "frameLossNearEndPercent", 8: "frameLossFarEndPercent"}
SESSION_TYPE = {1: "ethDm", 2: "ethSlm"}

SNMP_TRAP_OID = "1.3.6.1.6.3.1.1.4.1.0"

trap_count = 0
traps = []


def decode_ber_length(data, offset):
    b = data[offset]
    offset += 1
    if b & 0x80 == 0:
        return b, offset
    num_bytes = b & 0x7f
    length = 0
    for _ in range(num_bytes):
        length = (length << 8) | data[offset]
        offset += 1
    return length, offset


def decode_ber_oid(data, offset, length):
    end = offset + length
    if length == 0:
        return "0.0"
    first = data[offset]
    oid_parts = [str(first // 40), str(first % 40)]
    offset += 1
    val = 0
    while offset < end:
        b = data[offset]
        offset += 1
        val = (val << 7) | (b & 0x7f)
        if b & 0x80 == 0:
            oid_parts.append(str(val))
            val = 0
    return ".".join(oid_parts)


def decode_ber_integer(data, offset, length):
    val = 0
    for i in range(length):
        val = (val << 8) | data[offset + i]
    if length > 0 and data[offset] & 0x80:
        val -= (1 << (8 * length))
    return val


def decode_tlv(data, offset):
    if offset >= len(data):
        return None, None, offset
    tag = data[offset]
    offset += 1
    length, offset = decode_ber_length(data, offset)
    return tag, length, offset


def decode_varbind(data, offset):
    tag, length, offset = decode_tlv(data, offset)
    if tag != 0x30:
        return None, None, offset + (length if length else 0)
    vb_end = offset + length

    oid_tag, oid_len, offset = decode_tlv(data, offset)
    if oid_tag != 0x06:
        return None, None, vb_end
    oid_str = decode_ber_oid(data, offset, oid_len)
    offset += oid_len

    val_tag, val_len, offset = decode_tlv(data, offset)
    if val_tag == 0x02:  # INTEGER
        value = decode_ber_integer(data, offset, val_len)
    elif val_tag in (0x04, 0x40):  # OCTET STRING / Opaque
        value = data[offset:offset+val_len]
        try:
            value = value.decode('utf-8', errors='replace')
        except:
            value = value.hex()
    elif val_tag == 0x06:  # OID
        value = decode_ber_oid(data, offset, val_len)
    elif val_tag == 0x41:  # Counter32
        value = decode_ber_integer(data, offset, val_len)
    elif val_tag == 0x42:  # Unsigned32 / Gauge32
        value = decode_ber_integer(data, offset, val_len)
    elif val_tag == 0x43:  # TimeTicks
        value = decode_ber_integer(data, offset, val_len)
    elif val_tag == 0x05:  # NULL
        value = None
    else:
        value = f"<tag=0x{val_tag:02x} len={val_len}>"

    return oid_str, value, vb_end


def decode_snmpv2_trap(data):
    tag, length, offset = decode_tlv(data, 0)
    if tag != 0x30:
        return None

    ver_tag, ver_len, offset = decode_tlv(data, offset)
    version = decode_ber_integer(data, offset, ver_len)
    offset += ver_len

    comm_tag, comm_len, offset = decode_tlv(data, offset)
    community = data[offset:offset+comm_len].decode('utf-8', errors='replace')
    offset += comm_len

    pdu_tag, pdu_len, offset = decode_tlv(data, offset)
    pdu_end = offset + pdu_len

    req_tag, req_len, offset = decode_tlv(data, offset)
    request_id = decode_ber_integer(data, offset, req_len)
    offset += req_len

    err_tag, err_len, offset = decode_tlv(data, offset)
    error_status = decode_ber_integer(data, offset, err_len)
    offset += err_len

    idx_tag, idx_len, offset = decode_tlv(data, offset)
    error_index = decode_ber_integer(data, offset, idx_len)
    offset += idx_len

    vbl_tag, vbl_len, offset = decode_tlv(data, offset)
    vbl_end = offset + vbl_len

    varbinds = []
    while offset < vbl_end:
        oid, value, offset = decode_varbind(data, offset)
        if oid is not None:
            varbinds.append((oid, value))

    return {
        "version": version,
        "community": community,
        "pdu_type": pdu_tag,
        "request_id": request_id,
        "varbinds": varbinds
    }


def format_trap(trap_data, src_addr):
    global trap_count
    trap_count += 1

    timestamp = datetime.datetime.now().isoformat()
    trap_oid = None
    trap_name = "UNKNOWN"
    varbind_details = []

    for oid, value in trap_data["varbinds"]:
        name = OID_NAMES.get(oid, oid)
        if oid == SNMP_TRAP_OID:
            trap_oid = value if isinstance(value, str) else str(value)
            trap_name = OID_NAMES.get(trap_oid, trap_oid)
        else:
            display_val = value
            if name == "dnCfmTrapHighestDefectPri" and isinstance(value, int):
                display_val = f"{value} ({DEFECT_PRI.get(value, 'unknown')})"
            elif name == "dnCfmTrapThresholdType" and isinstance(value, int):
                display_val = f"{value} ({THRESHOLD_TYPE.get(value, 'unknown')})"
            elif name == "dnCfmTrapSessionType" and isinstance(value, int):
                display_val = f"{value} ({SESSION_TYPE.get(value, 'unknown')})"
            varbind_details.append({"oid": oid, "name": name, "value": display_val})

    record = {
        "trap_number": trap_count,
        "timestamp": timestamp,
        "source": f"{src_addr[0]}:{src_addr[1]}",
        "community": trap_data["community"],
        "trap_oid": trap_oid,
        "trap_name": trap_name,
        "varbinds": varbind_details
    }

    traps.append(record)

    print(f"\n{'='*70}")
    print(f"TRAP #{trap_count}  |  {timestamp}  |  from {src_addr[0]}:{src_addr[1]}")
    print(f"Community: {trap_data['community']}")
    print(f"Trap OID:  {trap_oid}")
    print(f"Trap Name: {trap_name}")
    print(f"Varbinds:")
    for vb in varbind_details:
        print(f"  {vb['name']:40s} = {vb['value']}")
    print(f"{'='*70}")
    sys.stdout.flush()

    with open(TRAP_LOG, 'w') as f:
        json.dump(traps, f, indent=2, default=str)

    return record


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(('0.0.0.0', LISTEN_PORT))

    print(f"SNMP Trap Listener started on UDP port {LISTEN_PORT}")
    print(f"Logging to {TRAP_LOG}")
    print(f"Waiting for traps from DUT 100.64.4.125 ...")
    print(f"Press Ctrl+C to stop")
    print()
    sys.stdout.flush()

    def signal_handler(sig, frame):
        print(f"\n\nStopping. Total traps received: {trap_count}")
        with open(TRAP_LOG, 'w') as f:
            json.dump(traps, f, indent=2, default=str)
        sock.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    while True:
        data, addr = sock.recvfrom(65535)
        try:
            trap_data = decode_snmpv2_trap(data)
            if trap_data:
                pdu_type = trap_data["pdu_type"]
                if pdu_type == 0xa7:  # SNMPv2-Trap-PDU
                    format_trap(trap_data, addr)
                else:
                    print(f"[{datetime.datetime.now().isoformat()}] Non-trap SNMP PDU (type=0x{pdu_type:02x}) from {addr[0]}")
                    sys.stdout.flush()
            else:
                print(f"[{datetime.datetime.now().isoformat()}] Failed to decode packet from {addr[0]} ({len(data)} bytes)")
                sys.stdout.flush()
        except Exception as e:
            print(f"[{datetime.datetime.now().isoformat()}] Error decoding packet from {addr[0]}: {e}")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
