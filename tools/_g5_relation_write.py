# -*- coding: utf-8 -*-
"""Goal5 controlled write: set local faction relation known flag +0x7e0 = 1.

Scope: local faction's diplomacy/relation manager only (Oda by default).
- --apply   : backup original +0x7e0 bytes, write 1, readback verify
- --restore : restore from latest backup JSON
- --status  : read-only summary of current known flags (no write)

Usage:
  python -u tools/_g5_relation_write.py --apply
  python -u tools/_g5_relation_write.py --restore
  python -u tools/_g5_relation_write.py --status
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import probe_battle_env as pb
import re_c2_faction as fc
import _probe_g5_relation_chain as chain

BACKUP_PATTERN = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".g5_relation_known_backup_*.json")


def list_backups():
    import glob
    return sorted(glob.glob(BACKUP_PATTERN), reverse=True)


def load_latest_backup():
    backs = list_backups()
    if not backs:
        print("!! no backup found")
        return None
    with open(backs[0], "r", encoding="utf-8") as f:
        return json.load(f)


def get_local_dm(h, base):
    """Return (local_faction, dm, cnt, dm_base) or None."""
    for rva, label in [(chain.RVA_SESSION_A, "A"), (chain.RVA_SESSION_B, "B")]:
        cm_local = chain.probe_session(h, base, rva, label)
        if cm_local:
            _, local = cm_local
            if chain.is_ptr(local):
                dm = chain.r32(h, local + chain.RVA_FACTION_DM)
                if chain.is_ptr(dm):
                    cnt = chain.r32(h, dm + chain.RVA_DM_CNT)
                    arr = chain.r32(h, dm + chain.RVA_DM_BASE)
                    if cnt is not None and 0 < cnt <= 0x10000 and chain.is_ptr(arr):
                        return local, dm, cnt, arr
    return None


def collect_entries(h, local, dm, cnt, arr):
    entries = []
    for i in range(cnt):
        rel = arr + i * chain.REL_STRIDE
        other = chain.r32(h, rel + chain.REL_OTHER)
        known = chain.r8(h, rel + chain.REL_KNOWN)
        name = None
        if chain.is_ptr(other):
            name = chain.read_utf16_name(h, chain.r32(h, other + chain.RVA_FACTION_NAME))
        entries.append({
            "idx": i,
            "rel": rel,
            "other": other,
            "name": name,
            "known": known,
        })
    return entries


def collect_all_entries(h, base, local):
    """Enumerate every faction's dm entries (from true campaign model +0x1498)."""
    model = chain.campaign_model_from_faction(h, local)
    if not chain.is_ptr(model):
        print("!! cannot get campaign model from local faction")
        return None
    fa = chain.r32(h, model + chain.RVA_CM_FACTION_ARR)
    if not chain.is_ptr(fa):
        print(f"!! [model+0x1498] invalid 0x{fa:08x}")
        return None
    fcnt = chain.r32(h, fa + chain.RVA_FACTION_ARR_CNT)
    farr = chain.r32(h, fa + chain.RVA_FACTION_ARR_PTR)
    if fcnt is None or fcnt > 0x1000 or not chain.is_ptr(farr):
        print(f"!! faction array invalid cnt={fcnt} arr=0x{farr:08x}")
        return None
    all_entries = []
    for i in range(fcnt):
        fp = chain.r32(h, farr + i * 4)
        if not chain.is_ptr(fp):
            continue
        dm = chain.r32(h, fp + chain.RVA_FACTION_DM)
        if not chain.is_ptr(dm):
            continue
        cnt = chain.r32(h, dm + chain.RVA_DM_CNT)
        arr = chain.r32(h, dm + chain.RVA_DM_BASE)
        if cnt is None or not (0 < cnt <= 0x10000) or not chain.is_ptr(arr):
            continue
        ents = collect_entries(h, fp, dm, cnt, arr)
        all_entries.extend(ents)
    return all_entries


def write_entries(h, entries, label, local=None, dm=None, base=None):
    backup = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scope": label,
        "base": base,
        "local": local,
        "dm": dm,
        "entries": [{
            "idx": e["idx"],
            "rel": e["rel"],
            "other": e["other"],
            "name": e["name"],
            "known_before": e["known"],
        } for e in entries],
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        f".g5_relation_known_backup_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False, indent=2)
    print(f"backup -> {path}")

    changed = 0
    failed = []
    for e in entries:
        if e["known"] == 1:
            continue
        addr = e["rel"] + chain.REL_KNOWN
        ok = pb.write_u8(h, addr, 1)
        if ok:
            changed += 1
        else:
            failed.append((addr, e["name"]))
    ok_cnt = 0
    for e in entries:
        v = chain.r8(h, e["rel"] + chain.REL_KNOWN)
        if v == 1:
            ok_cnt += 1
        else:
            failed.append((e["rel"] + chain.REL_KNOWN, e["name"]))
    print(f"[{label}] entries={len(entries)} written_new=1:{changed} readback_known==1:{ok_cnt}/{len(entries)}")
    if failed:
        print(f"!! failures: {failed[:10]}")
        return 2
    print(f"OK [{label}] all relation known flags = 1")
    return 0


def apply(h, base, all_dm=False):
    got = get_local_dm(h, base)
    if not got:
        print("!! cannot locate local dm chain")
        return 1
    local, dm, cnt, arr = got
    print(f"local faction=0x{local:08x} dm=0x{dm:08x} count={cnt} array=0x{arr:08x}")

    if all_dm:
        entries = collect_all_entries(h, base, local)
        if entries is None:
            return 1
        return write_entries(h, entries, "all-dm", local=local, dm=dm, base=base)
    else:
        entries = collect_entries(h, local, dm, cnt, arr)
    backup = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "base": base,
        "local": local,
        "dm": dm,
        "entries": [{
            "idx": e["idx"],
            "rel": e["rel"],
            "other": e["other"],
            "name": e["name"],
            "known_before": e["known"],
        } for e in entries],
    }
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        f".g5_relation_known_backup_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False, indent=2)
    print(f"backup -> {path}")

    changed = 0
    failed = []
    for e in entries:
        if e["known"] == 1:
            continue
        addr = e["rel"] + chain.REL_KNOWN
        ok = pb.write_u8(h, addr, 1)
        if ok:
            changed += 1
        else:
            failed.append((addr, e["name"]))
    # readback
    ok_cnt = 0
    for e in entries:
        v = chain.r8(h, e["rel"] + chain.REL_KNOWN)
        if v == 1:
            ok_cnt += 1
        else:
            failed.append((e["rel"] + chain.REL_KNOWN, e["name"]))
    print(f"written new 1: {changed}, readback known==1: {ok_cnt}/{len(entries)}")
    if failed:
        print(f"!! failures: {failed[:10]}")
        return 2
    print("OK all local relation known flags = 1")
    return 0


def restore(h, base, force=False):
    backup = load_latest_backup()
    if not backup:
        return 1
    print(f"restoring backup: {list_backups()[0]}")
    print(f"timestamp={backup['timestamp']} scope={backup.get('scope')} entries={len(backup['entries'])}")
    if 'local' in backup:
        print(f"backup local=0x{backup['local']:08x} dm=0x{backup['dm']:08x}")
        got = get_local_dm(h, base)
        if got:
            cur_local, cur_dm, _, _ = got
            if cur_local != backup['local'] or cur_dm != backup['dm']:
                print(f"!! session mismatch: current local=0x{cur_local:08x} dm=0x{cur_dm:08x}")
                print(f"!! backup is from a different campaign/session; refusing stale restore.")
                print(f"!! use --force to override (dangerous, may write to reused addresses).")
                if not force:
                    return 3
    ok = 0
    failed = []
    for e in backup["entries"]:
        addr = e["rel"] + chain.REL_KNOWN
        val = int(e["known_before"])
        if pb.write_u8(h, addr, val):
            ok += 1
        else:
            failed.append((addr, e["name"]))
    # readback
    bad = []
    for e in backup["entries"]:
        v = chain.r8(h, e["rel"] + chain.REL_KNOWN)
        if v != e["known_before"]:
            bad.append((e["rel"] + chain.REL_KNOWN, e["name"], v, e["known_before"]))
    print(f"restored {ok}/{len(backup['entries'])}")
    if bad:
        print(f"!! readback mismatch: {bad[:10]}")
        return 2
    print("OK restored")
    return 0


def status(h, base):
    got = get_local_dm(h, base)
    if not got:
        print("!! cannot locate local dm chain")
        return 1
    local, dm, cnt, arr = got
    entries = collect_entries(h, local, dm, cnt, arr)
    known = sum(1 for e in entries if e["known"] == 1)
    print(f"local=0x{local:08x} dm=0x{dm:08x} count={cnt} known={known}/{len(entries)}")
    for e in entries:
        print(f"  #{e['idx']:3d} rel=0x{e['rel']:08x} other=0x{e['other']:08x} known={e['known']} name={e['name']!r}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write relation known=1 with backup")
    ap.add_argument("--all", action="store_true", help="with --apply: write every faction dm (default: local dm only)")
    ap.add_argument("--restore", action="store_true", help="restore latest backup")
    ap.add_argument("--force", action="store_true", help="with --restore: bypass session mismatch guard")
    ap.add_argument("--status", action="store_true", help="read-only status")
    args = ap.parse_args()

    h, base = fc.open_game()
    if not h:
        return 1

    if args.apply:
        return apply(h, base, all_dm=args.all)
    if args.restore:
        return restore(h, base, force=args.force)
    if args.status:
        return status(h, base)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
