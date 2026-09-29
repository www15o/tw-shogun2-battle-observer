# -*- coding: utf-8 -*-
"""Goal5 live read-only probe: session -> campaign model -> local faction -> dm -> relations.

Dumps:
- session anchors (0x1bc8fe4 / 0x1bc8fe0)
- campaign model + local faction
- local diplomacy/relation manager
- each relation: other faction, +0x708 (new-contact), +0x7e0 (known), +0x710 stance ptr/id

Usage:
  python -u tools/_probe_g5_relation_chain.py [--max-rel 200] [--all-dm]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_battle_env as pb
import re_c2_faction as fc

RVA_SESSION_A = 0x1bc8fe4
RVA_SESSION_B = 0x1bc8fe0
RVA_CM_LOCAL = 0xa98          # [session+0xa98] -> campaign model
RVA_CM_B0 = 0xb0              # [cm+0xb0] -> local faction
RVA_CM_FACTION_ARR = 0x1498   # [cm+0x1498] -> faction array object
RVA_FACTION_ARR_CNT = 0x40
RVA_FACTION_ARR_PTR = 0x44
RVA_FACTION_DM = 0x53c
RVA_FACTION_NAME = 0x0b14
RVA_DM_CNT = 0x8
RVA_DM_BASE = 0xc
REL_STRIDE = 0x7e4
REL_OTHER = 0x4
REL_NEW = 0x708
REL_KNOWN = 0x7e0
REL_STANCE = 0x710
STANCE_ID = 0x10


def is_ptr(v):
    return isinstance(v, int) and 0x10000 <= v <= 0x7fffffff and (v & 0xfff) != 0xfffff


def r32(h, a):
    return pb.read_u32(h, a)


def r8(h, a):
    b = pb.read_mem(h, a, 1)
    return b[0] if b else None


def read_utf16_name(h, addr):
    if not is_ptr(addr):
        return None
    b = pb.read_mem(h, addr, 64)
    if not b:
        return None
    try:
        s = b.decode("utf-16-le").split("\x00")[0]
        return s if s and s.isprintable() else None
    except Exception:
        return None


def probe_session(h, base, rva, label):
    print(f"\n=== session anchor {label} RVA 0x{rva:x} ===")
    s = r32(h, base + rva)
    if not is_ptr(s):
        print(f"  [base+0x{rva:x}] = {s} -> not a pointer")
        return None
    print(f"  [base+0x{rva:x}] = 0x{s:08x}")
    cm = r32(h, s + RVA_CM_LOCAL)
    if not is_ptr(cm):
        print(f"  [session+0xa98] = {cm} -> not a pointer")
        return None
    print(f"  [session+0xa98] campaign model = 0x{cm:08x}")
    local = r32(h, cm + RVA_CM_B0)
    print(f"  [cm+0xb0] local faction = 0x{local:08x}" if is_ptr(local) else
          f"  [cm+0xb0] local faction = {local} -> not a pointer")
    return cm, local


def dump_faction_brief(h, addr):
    v0 = r32(h, addr)
    name = read_utf16_name(h, r32(h, addr + RVA_FACTION_NAME))
    h6 = r8(h, addr + 0x6a0)
    h7 = r8(h, addr + 0x7a8)
    dm = r32(h, addr + RVA_FACTION_DM)
    print(f"    faction 0x{addr:08x} vtable=0x{v0:08x} human={h6} handed={h7} name={name!r} dm=0x{dm:08x}")
    return name


def campaign_model_from_faction(h, faction):
    """faction+0x8c -> [+8] -> campaign model (L15 chain)."""
    p1 = r32(h, faction + 0x8c)
    if not is_ptr(p1):
        return None
    cm = r32(h, p1 + 8)
    return cm if is_ptr(cm) else None


def dump_dm(h, dm, max_rel):
    print(f"\n=== diplomacy/relation manager 0x{dm:08x} ===")
    cnt = r32(h, dm + RVA_DM_CNT)
    base = r32(h, dm + RVA_DM_BASE)
    print(f"  count=0x{cnt:x} ({cnt})  array=0x{base:08x}  stride=0x{REL_STRIDE:x}")
    if not is_ptr(base) or cnt is None or cnt > 0x10000:
        print("  invalid dm/count/base, skip")
        return
    known_cnt = 0
    new_cnt = 0
    for i in range(min(cnt, max_rel)):
        rel = base + i * REL_STRIDE
        other = r32(h, rel + REL_OTHER)
        known = r8(h, rel + REL_KNOWN)
        new708 = r8(h, rel + REL_NEW)
        stance = r32(h, rel + REL_STANCE)
        sid = r32(h, stance + STANCE_ID) if is_ptr(stance) else None
        name = read_utf16_name(h, r32(h, other + RVA_FACTION_NAME)) if is_ptr(other) else None
        if known == 1:
            known_cnt += 1
        if new708 == 1:
            new_cnt += 1
        print(f"  #{i:3d} rel=0x{rel:08x} other=0x{other:08x} known(+0x7e0)={known} "
              f"new(+0x708)={new708} stance_ptr=0x{stance:08x} stance_id={sid} other_name={name!r}")
    print(f"  -> known={known_cnt}/{min(cnt, max_rel)}  new708={new_cnt}/{min(cnt, max_rel)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-rel", type=int, default=200)
    ap.add_argument("--all-dm", action="store_true", help="dump all faction DMs (default: local only)")
    args = ap.parse_args()

    h, base = fc.open_game()
    if not h:
        return 1

    cm_local = None
    for rva, label in [(RVA_SESSION_A, "A"), (RVA_SESSION_B, "B")]:
        cm_local = probe_session(h, base, rva, label)
        if cm_local:
            break
    if not cm_local:
        print("\n!! session/campaign model chain failed, fallback to faction scan")
        facs = fc.scan(h, base, human_only=False, max_cands=60)
        print(f"  scanned factions: {len(facs)}")
        for a in facs[:10]:
            dump_faction_brief(h, a)
        return 0

    cm, local = cm_local
    cm2 = campaign_model_from_faction(h, local) if is_ptr(local) else None
    cm_use = cm2 if is_ptr(cm2) else cm
    print("\n=== campaign model ===")
    print(f"  session+0xa98 object cm=0x{cm:08x}")
    print(f"  faction+0x8c->[+8] campaign model cm2=0x{cm2:08x}" if is_ptr(cm2) else
          f"  faction+0x8c->[+8] campaign model cm2={cm2}")
    fa = r32(h, cm_use + RVA_CM_FACTION_ARR)
    print(f"  using cm_use=0x{cm_use:08x}")
    if is_ptr(fa):
        fcnt = r32(h, fa + RVA_FACTION_ARR_CNT)
        farr = r32(h, fa + RVA_FACTION_ARR_PTR)
        print(f"  [cm_use+0x1498]=0x{fa:08x} cnt=0x{fcnt:x} arr=0x{farr:08x}")
        if is_ptr(farr) and fcnt is not None and fcnt <= 0x1000:
            print("  faction array:")
            for i in range(min(fcnt, 60)):
                fp = r32(h, farr + i * 4)
                if not is_ptr(fp):
                    continue
                name = dump_faction_brief(h, fp)
                if name == "织田" or (local and fp == local):
                    print(f"    ^^^ local={'*' if fp == local else ''}")
    else:
        print("  [cm_use+0x1498] invalid")

    if is_ptr(local):
        print("\n=== local faction ===")
        dump_faction_brief(h, local)
        dm = r32(h, local + RVA_FACTION_DM)
        if is_ptr(dm):
            dump_dm(h, dm, args.max_rel)
        else:
            print(f"  local dm invalid: 0x{dm:08x}")

    if args.all_dm and is_ptr(fa) and is_ptr(farr) and fcnt is not None and fcnt <= 0x1000:
        print("\n=== all faction DMs ===")
        for i in range(fcnt):
            fp = r32(h, farr + i * 4)
            if not is_ptr(fp):
                continue
            dm = r32(h, fp + RVA_FACTION_DM)
            if is_ptr(dm):
                name = read_utf16_name(h, r32(h, fp + RVA_FACTION_NAME))
                print(f"\n-- [{i}] {name!r} faction=0x{fp:08x} dm=0x{dm:08x}")
                dump_dm(h, dm, args.max_rel)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
