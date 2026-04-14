#!/usr/bin/env python3
"""
NETCONF: trigger BGP sr-labeled-unicast bgp-prefix-sid-map must
(global-block-origination-in without bgp-prefix-sid-map-policy-in).

Usage:
  python3 netconf_prefix_sid_test.py [host] [port]

Default port 830. Requires TCP reachability to the NETCONF port (often blocked
from off-lab networks even when SSH :22 works). Device must have
  system netconf port <port>
and the NETCONF server listening (see show config system netconf).

SSH port 22 uses the interactive CLI subsystem only; ncclient needs the
dedicated NETCONF port (default 830 per DNOS).
"""
import sys
from ncclient import manager
from ncclient.operations import RaiseMode
from ncclient.xml_ import to_ele

HOST = sys.argv[1] if len(sys.argv) > 1 else "wky1c7vd00008p2.dev.drivenets.net"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 830

# Orphan global-block only (no policy-in leaf) — expect dn-ex:err-msg text.
CONFIG_XML = """
<config xmlns:dn-top="http://drivenets.com/ns/yang/dn-top"
        xmlns:dn-ns="http://drivenets.com/ns/yang/dn-network-services"
        xmlns:dn-vrf="http://drivenets.com/ns/yang/dn-vrf"
        xmlns:dn-bgp="http://drivenets.com/ns/yang/dn-bgp">
  <dn-top:drivenets-top>
    <dn-ns:network-services>
      <dn-vrf:vrfs>
        <vrf>
          <vrf-name>default</vrf-name>
          <config-items>
            <vrf-name>default</vrf-name>
          </config-items>
          <protocols>
            <dn-bgp:bgp>
              <as-number>65242</as-number>
              <global>
                <global-config>
                  <as-number>65242</as-number>
                </global-config>
              </global>
              <neighbors>
                <neighbor>
                  <neighbor-address>10.99.99.99</neighbor-address>
                  <config-items>
                    <neighbor-address>10.99.99.99</neighbor-address>
                    <remote-as>9999</remote-as>
                  </config-items>
                  <address-families>
                    <neighbor-address-family>
                      <address-family-type>ipv4-labeled-unicast</address-family-type>
                      <config-items>
                        <address-family-type>ipv4-labeled-unicast</address-family-type>
                        <neighbor-unicast-afi-config>
                          <sr-labeled-unicast>
                            <bgp-prefix-sid-map>
                              <global-block-origination-in/>
                            </bgp-prefix-sid-map>
                          </sr-labeled-unicast>
                        </neighbor-unicast-afi-config>
                      </config-items>
                    </neighbor-address-family>
                  </address-families>
                </neighbor>
              </neighbors>
            </dn-bgp:bgp>
          </protocols>
        </vrf>
      </dn-vrf:vrfs>
    </dn-ns:network-services>
  </dn-top:drivenets-top>
</config>
""".strip()


def main():
    print(f"Connecting NETCONF to {HOST}:{PORT} ...")
    with manager.connect(
        host=HOST,
        port=PORT,
        username="dnroot",
        password="dnroot",
        hostkey_verify=False,
        allow_agent=False,
        look_for_keys=False,
        timeout=90,
    ) as m:
        m.raise_mode = RaiseMode.NONE
        caps = [c for c in m.server_capabilities]
        print("Server capabilities (first 5):", caps[:5], "...")

        if "urn:ietf:params:netconf:capability:candidate:1.0" not in m.server_capabilities:
            print("WARNING: candidate datastore not advertised; trying running merge.")

        try:
            m.lock("candidate")
        except Exception as e:
            print("lock candidate:", e)

        try:
            m.discard_changes()
        except Exception as e:
            print("discard_changes:", e)

        expect = "global-block-origination-in requires bgp-prefix-sid-map-policy-in"

        print("edit-config merge (orphan global-block-origination-in) ...")
        er = m.edit_config(target="candidate", config=CONFIG_XML, default_operation="merge")
        exml = er.xml
        print("edit-config reply:", exml)
        ok = expect.lower() in exml.lower()

        if not ok:
            print("validate candidate ...")
            val = m.dispatch(
                to_ele(
                    '<validate xmlns="urn:ietf:params:xml:ns:netconf:base:1.0">'
                    "<source><candidate/></source></validate>"
                )
            )
            vxml = val.xml
            print("validate reply:", vxml)
            ok = expect.lower() in vxml.lower()

        if not ok:
            print("trying commit for error text ...")
            cr = m.commit()
            cxml = cr.xml
            print("commit reply:", cxml)
            ok = expect.lower() in cxml.lower()

        print("\n--- RESULT ---")
        if ok:
            print(f"PASS: RPC output contains:\n  {expect!r}")
        else:
            print(f"FAIL: did not find:\n  {expect!r}")

        try:
            m.discard_changes()
        except Exception:
            pass
        try:
            m.unlock("candidate")
        except Exception:
            pass

        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
