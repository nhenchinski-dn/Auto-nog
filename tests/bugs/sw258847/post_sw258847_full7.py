#!/usr/bin/env python3
"""Build ADF comment body for SW-258847 with full 7-step outputs per variant."""
import json

ADF = {"type": "doc", "version": 1, "content": []}


def h(level, txt):
    ADF["content"].append({
        "type": "heading", "attrs": {"level": level},
        "content": [{"type": "text", "text": txt}],
    })


def p(*parts):
    content = []
    for part in parts:
        if isinstance(part, str):
            content.append({"type": "text", "text": part})
        else:
            content.append(part)
    ADF["content"].append({"type": "paragraph", "content": content})


def bold(txt):
    return {"type": "text", "text": txt, "marks": [{"type": "strong"}]}


def code(txt):
    return {"type": "text", "text": txt, "marks": [{"type": "code"}]}


def cb(txt):
    ADF["content"].append({
        "type": "codeBlock",
        "content": [{"type": "text", "text": txt}],
    })


def rule():
    ADF["content"].append({"type": "rule"})


# ---------- Header ----------
h(2, "Full 7-step lifecycle per variant — test outputs")
p("Each variant runs all 7 Jira test steps end-to-end. Each variant is "
  "isolated in its own test VRF so Step 5 (enable allow-default on one AFI) "
  "does not collide with other uRPF interfaces in the same VRF. For every "
  "step, ", code("show interfaces <IF>"), " and ",
  code("show interfaces detail <IF>"),
  " produced identical uRPF lines (Pass Criterion #2).")

p(bold("Legend:"),
  " S1 = apply starting config, S2/S3 = show brief / show detail verification,"
  " S4 = modify a single AFI mode, S5 = enable allow-default on one AFI,"
  " S6 = delete per-AFI config (fall back to global), S7 = delete all uRPF"
  " (both AFIs disabled).")

# ---------- Per-variant sections ----------
variants = [
    ("V1 — ge sub-interface ge400-0/0/34.100 (global-only strict, VRF urpf_v1_vrf)", [
        ("S1: admin-state enabled / mode strict / allow-default disabled",
         "uRPF IPv4 check: enabled, Mode: strict, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: strict, Allow-default: disabled"),
        ("S4: change global mode strict -> loose",
         "uRPF IPv4 check: enabled, Mode: loose, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: loose, Allow-default: disabled"),
        ("S5: enable v6 per-AFI admin-state enabled + allow-default enabled",
         "uRPF IPv4 check: enabled, Mode: loose, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: loose, Allow-default: enabled"),
        ("S6: no urpf address-family ipv6 (fall back to global)",
         "uRPF IPv4 check: enabled, Mode: loose, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: loose, Allow-default: disabled"),
        ("S7: no urpf (disabled)",
         "uRPF IPv4 check: disabled\nuRPF IPv6 check: disabled"),
    ]),
    ("V2 — bundle-99 (global-only loose, VRF urpf_v2_vrf)", [
        ("S1: admin-state enabled / mode loose / allow-default disabled",
         "uRPF IPv4 check: enabled, Mode: loose, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: loose, Allow-default: disabled"),
        ("S4: change global mode loose -> strict",
         "uRPF IPv4 check: enabled, Mode: strict, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: strict, Allow-default: disabled"),
        ("S5: enable v6 per-AFI admin-state + allow-default "
         "(per-AFI mode unspecified -> defaults to loose, overrides global)",
         "uRPF IPv4 check: enabled, Mode: strict, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: loose,  Allow-default: enabled"),
        ("S6: no urpf address-family ipv6",
         "uRPF IPv4 check: enabled, Mode: strict, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: strict, Allow-default: disabled"),
        ("S7: no urpf",
         "uRPF IPv4 check: disabled\nuRPF IPv6 check: disabled"),
    ]),
    ("V3 — bundle sub-interface bundle-99.200 (per-AFI only, VRF urpf_v3_vrf)", [
        ("S1: urpf admin-state enabled + v4 strict + v6 loose (no global mode)",
         "uRPF IPv4 check: enabled, Mode: strict, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: loose,  Allow-default: disabled"),
        ("S4: modify v6 per-AFI mode loose -> strict",
         "uRPF IPv4 check: enabled, Mode: strict, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: strict, Allow-default: disabled"),
        ("S5: enable v6 per-AFI allow-default enabled",
         "uRPF IPv4 check: enabled, Mode: strict, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: strict, Allow-default: enabled"),
        ("S6: no urpf address-family ipv6 (v4 per-AFI remains)",
         "uRPF IPv4 check: enabled, Mode: strict, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: loose,  Allow-default: disabled"),
        ("S7: no urpf",
         "uRPF IPv4 check: disabled\nuRPF IPv6 check: disabled"),
    ]),
    ("V4 — irb99 (IRB, loose only — strict unsupported on IRB, VRF urpf_v4_vrf)", [
        ("S1: admin-state enabled / mode loose / AD disabled + v6 per-AFI admin enabled",
         "uRPF IPv4 check: enabled, Mode: loose, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: loose, Allow-default: disabled"),
        ("S4 (adapted): v6 per-AFI allow-default disabled -> enabled "
         "(mode flip unavailable — IRB rejects strict)",
         "uRPF IPv4 check: enabled, Mode: loose, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: loose, Allow-default: enabled"),
        ("S5: v4 per-AFI admin-state enabled (inherits global loose / AD disabled)",
         "uRPF IPv4 check: enabled, Mode: loose, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: loose, Allow-default: enabled"),
        ("S6: no urpf address-family ipv6",
         "uRPF IPv4 check: enabled, Mode: loose, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: loose, Allow-default: disabled"),
        ("S7: no urpf",
         "uRPF IPv4 check: disabled\nuRPF IPv6 check: disabled"),
    ]),
    ("V5 — ge400-0/0/34 in VRF test (allow-default enabled, strict)", [
        ("S1: admin-state enabled / mode strict / allow-default enabled",
         "uRPF IPv4 check: enabled, Mode: strict, Allow-default: enabled\n"
         "uRPF IPv6 check: enabled, Mode: strict, Allow-default: enabled"),
        ("S4: change global mode strict -> loose",
         "uRPF IPv4 check: enabled, Mode: loose, Allow-default: enabled\n"
         "uRPF IPv6 check: enabled, Mode: loose, Allow-default: enabled"),
        ("S5: add v6 per-AFI admin-state + allow-default enabled",
         "uRPF IPv4 check: enabled, Mode: loose, Allow-default: enabled\n"
         "uRPF IPv6 check: enabled, Mode: loose, Allow-default: enabled"),
        ("S6: no urpf address-family ipv6",
         "uRPF IPv4 check: enabled, Mode: loose, Allow-default: enabled\n"
         "uRPF IPv6 check: enabled, Mode: loose, Allow-default: enabled"),
        ("S7: no urpf",
         "uRPF IPv4 check: disabled\nuRPF IPv6 check: disabled"),
    ]),
    ("V6 — ge400-0/0/34 (global-only loose, VRF test)", [
        ("S1: admin-state enabled / mode loose / allow-default disabled",
         "uRPF IPv4 check: enabled, Mode: loose, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: loose, Allow-default: disabled"),
        ("S4: change global mode loose -> strict",
         "uRPF IPv4 check: enabled, Mode: strict, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: strict, Allow-default: disabled"),
        ("S5: v6 per-AFI admin-state + allow-default enabled "
         "(per-AFI mode unspecified -> defaults to loose, overrides global)",
         "uRPF IPv4 check: enabled, Mode: strict, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: loose,  Allow-default: enabled"),
        ("S6: no urpf address-family ipv6",
         "uRPF IPv4 check: enabled, Mode: strict, Allow-default: disabled\n"
         "uRPF IPv6 check: enabled, Mode: strict, Allow-default: disabled"),
        ("S7: no urpf",
         "uRPF IPv4 check: disabled\nuRPF IPv6 check: disabled"),
    ]),
]

for title, steps in variants:
    rule()
    h(3, title)
    for step_title, show_out in steps:
        p(bold(step_title))
        cb(show_out)

# ---------- Summary ----------
rule()
h(3, "Summary")
ADF["content"].append({
    "type": "bulletList",
    "content": [
        {"type": "listItem", "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "All 6 variants x 7 steps: PASS. "
             "show interfaces and show interfaces detail produced matching "
             "uRPF lines at every step."}]}]},
        {"type": "listItem", "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "V2 & V6 at S5: adding a per-AFI block "
             "without explicit "},
            code("mode"),
            {"type": "text", "text": " silently flips that AFI to "},
            code("loose"),
            {"type": "text", "text": " (per-AFI default), overriding the "
             "global "},
            code("strict"),
            {"type": "text", "text": ". "},
            code("show config"),
            {"type": "text", "text": " omits the defaulted value — worth "
             "flagging as a usability gap."}]}]},
        {"type": "listItem", "content": [{"type": "paragraph", "content": [
            {"type": "text", "text": "V4 adapted: "},
            code("mode strict"),
            {"type": "text", "text": " is rejected on IRB — documented "
             "platform gap."}]}]},
    ],
})

with open("/home/dn/sw258847_comment_full7.json", "w") as f:
    json.dump(ADF, f, indent=2)

print("OK")
