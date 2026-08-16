#!/usr/bin/env python3
"""memscan.py — 幕府2 32位进程内存扫描/读取/写入工具（R1a 用）。

目标：定位「军队级 AI active 标志（0/1/2 小整数）」—— M2TW EOP setAiActiveSet
在幕府2 同族引擎中的对应物，验证引擎是否允许玩家军队在战斗中交原生战斗 AI。

原理：ReadProcessMemory 全进程扫描已知可辨识值（士兵数/坐标/经验），
多轮差分收敛；WriteProcessMemory 翻转候选字节验证。

用法：
  python tools/memscan.py attach                          # 找到游戏进程
  python tools/memscan.py scan <value> [--type int32|float] [--file r.json] [--align 1|4]
  python tools/memscan.py rescan <value> --prev r.json [--type ...] [--file r2.json]
  python tools/memscan.py read <hexaddr> [--type int32|float|bytes] [--count n]
  python tools/memscan.py write <hexaddr> <value> [--type int32|float] [--yes]

安全：默认只读；write 需显式 --yes（且需提级运行，进程需同权限或管理员）。
注意：32位进程地址用 int 表示即可；扫描 2GB 空间分块读，步长 1 全偏移约几十秒。
"""
import argparse
import ctypes
import json
import sys
from ctypes import wintypes

# --- Win32 API 常量 ---
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008

MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_NOACCESS = 0x01
PAGE_READONLY = 0x02
PAGE_READWRITE = 0x04
PAGE_EXECUTE_READWRITE = 0x40
PAGE_GUARD = 0x100
READABLE_PROTECT = PAGE_READONLY | PAGE_READWRITE | PAGE_EXECUTE_READWRITE

k32 = ctypes.windll.kernel32
PROCESS_NAME = "shogun2.exe"


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", wintypes.LPVOID),
        ("AllocationBase", wintypes.LPVOID),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


def find_pid():
    """找 shogun2 进程 PID（无 ctypes 依赖的 psutil）。"""
    import subprocess
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {PROCESS_NAME}", "/FO", "CSV", "/NH"],
                         capture_output=True).stdout
    out = out.decode("oem", errors="replace")
    for line in out.strip().splitlines():
        parts = line.strip('"').split('","')
        if len(parts) >= 2 and parts[0].lower() == PROCESS_NAME.lower():
            return int(parts[1])
    return None


def open_process(pid):
    k32.OpenProcess.restype = wintypes.HANDLE
    h = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ | PROCESS_VM_WRITE | PROCESS_VM_OPERATION,
                        False, pid)
    if not h:
        err = ctypes.get_last_error()
        if err == 5:
            sys.exit("权限不足（error 5）：请用管理员/提级方式运行本工具。")
        sys.exit(f"OpenProcess 失败 error={err}")
    return h


def regions(h, readable_only=True):
    """枚举进程内存区域（MEM_COMMIT，可读保护）。"""
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    while True:
        size = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if size == 0:
            break
        if (mbi.State == MEM_COMMIT
                and (not readable_only or (mbi.Protect & ~PAGE_GUARD) & READABLE_PROTECT)
                and mbi.Protect != PAGE_NOACCESS):
            yield int(mbi.BaseAddress or addr), mbi.RegionSize
        addr += mbi.RegionSize
        if addr >= 0x80000000:  # 32位用户空间上限（未 LAA 时 0x7FFF0000 附近）
            break


def read_mem(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    nread = ctypes.c_size_t(0)
    ok = k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(nread))
    if not ok:
        return None
    return buf.raw[:nread.value]


def scan(h, value, typ, align, readable_only=True, progress=True):
    """全进程扫描 value（int32/float），返回地址列表。"""
    import struct
    width = 4
    if typ == "int32":
        pat = struct.pack("<i", value)
        unpack = lambda b: struct.unpack("<i", b)[0]
    elif typ == "float":
        pat = struct.pack("<f", value)
        unpack = lambda b: struct.unpack("<f", b)[0]
    else:
        sys.exit(f"unknown type {typ}")
    hits = []
    total = 0
    for base, size in regions(h, readable_only):
        total += size
    done = 0
    for base, size in regions(h, readable_only):
        # 分块读（8MB/块），Python 内搜索
        chunk = 8 * 1024 * 1024
        for off in range(0, size, chunk):
            sz = min(chunk, size - off)
            data = read_mem(h, base + off, sz)
            if data is None:
                continue
            step = max(1, align)
            start = 0 if align == 1 else 0
            for i in range(0, len(data) - width + 1, step):
                if data[i:i + width] == pat:
                    hits.append(base + off + i)
            done += sz
            if progress and done % (64 * 1024 * 1024) < chunk:
                print(f"\rscanned {done/1048576:.0f}/{total/1048576:.0f} MB, hits={len(hits)}", end="", flush=True)
    if progress:
        print()
    return hits


def rescan(h, value, prev_addrs, typ):
    """对上一轮地址列表重读，保留仍匹配的（差分收敛）。"""
    import struct
    if typ == "int32":
        unpack = lambda b: struct.unpack("<i", b)[0]
    elif typ == "float":
        unpack = lambda b: struct.unpack("<f", b)[0]
    else:
        sys.exit(f"unknown type {typ}")
    hits = []
    for a in prev_addrs:
        data = read_mem(h, a, 4)
        if data is not None and len(data) == 4:
            try:
                if unpack(data) == value:
                    hits.append(a)
            except struct.error:
                pass
    return hits


def read_addr(h, addr, typ, count):
    if typ == "bytes":
        data = read_mem(h, addr, count)
        return data.hex() if data else None
    if typ in ("int32", "float"):
        import struct
        out = []
        for i in range(count):
            data = read_mem(h, addr + i * 4, 4)
            if data is None:
                break
            out.append(struct.unpack("<i" if typ == "int32" else "<f", data)[0])
        return out
    sys.exit(f"unknown type {typ}")


def write_addr(h, addr, value, typ):
    import struct
    if typ == "int32":
        payload = struct.pack("<i", value)
    elif typ == "float":
        payload = struct.pack("<f", value)
    else:
        sys.exit(f"unknown type {typ}")
    nw = ctypes.c_size_t(0)
    ok = k32.WriteProcessMemory(h, ctypes.c_void_p(addr), payload, len(payload), ctypes.byref(nw))
    return bool(ok), nw.value


def load_prev(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save(path, addrs):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(addrs, f)


def main():
    ap = argparse.ArgumentParser(description="幕府2 内存扫描器（R1a）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("attach")
    p = sub.add_parser("scan")
    p.add_argument("value", type=float)
    p.add_argument("--type", default="int32", choices=["int32", "float"])
    p.add_argument("--align", type=int, default=1, help="1=全偏移（慢），4=对齐（快）")
    p.add_argument("--file", default=None, help="保存命中列表 JSON")
    p.add_argument("--pid", type=int, default=None)

    p = sub.add_parser("rescan")
    p.add_argument("value", type=float)
    p.add_argument("--prev", required=True)
    p.add_argument("--type", default="int32", choices=["int32", "float"])
    p.add_argument("--file", default=None)
    p.add_argument("--pid", type=int, default=None)

    p = sub.add_parser("read")
    p.add_argument("addr", type=lambda s: int(s, 16))
    p.add_argument("--type", default="int32", choices=["int32", "float", "bytes"])
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--pid", type=int, default=None)

    p = sub.add_parser("write")
    p.add_argument("addr", type=lambda s: int(s, 16))
    p.add_argument("value", type=float)
    p.add_argument("--type", default="int32", choices=["int32", "float"])
    p.add_argument("--yes", action="store_true", help="确认写入（危险操作）")
    p.add_argument("--pid", type=int, default=None)

    a = ap.parse_args()

    if a.cmd == "attach":
        pid = find_pid()
        if not pid:
            sys.exit("shogun2 未在运行")
        print(f"PID={pid}")
        return

    pid = a.pid or find_pid()
    if not pid:
        sys.exit("shogun2 未在运行")
    h = open_process(pid)
    print(f"attached PID={pid}")

    if a.cmd == "scan":
        hits = scan(h, int(a.value) if a.type == "int32" else a.value, a.type, a.align)
        print(f"hits={len(hits)}")
        if a.file:
            save(a.file, hits)
            print(f"saved {a.file}")
        else:
            for x in hits[:50]:
                print(f"  0x{x:08x}")
            if len(hits) > 50:
                print(f"  ... {len(hits)-50} more")

    elif a.cmd == "rescan":
        prev = load_prev(a.prev)
        hits = rescan(h, int(a.value) if a.type == "int32" else a.value, prev, a.type)
        print(f"prev={len(prev)} -> hits={len(hits)}")
        if a.file:
            save(a.file, hits)
            print(f"saved {a.file}")
        else:
            for x in hits[:50]:
                print(f"  0x{x:08x}")

    elif a.cmd == "read":
        r = read_addr(h, a.addr, a.type, a.count)
        print(r)

    elif a.cmd == "write":
        if not a.yes:
            sys.exit("写内存需 --yes 确认（且需提级运行）")
        ok, n = write_addr(h, a.addr, int(a.value) if a.type == "int32" else a.value, a.type)
        print(f"write ok={ok} bytes={n}")


if __name__ == "__main__":
    main()
