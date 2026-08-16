# -*- coding: utf-8 -*-
"""Empire.Retail.dll 静态 RE 分析公共库（T1-T5 复用）。

- PE 段/导入/导出解析
- RVA <-> 文件偏移互转
- capstone 反汇编（32 位 x86）
- 全模式 xref：push/mov/lea/cmp imm32 直接引用 + E8 相对 call 目标
- 字符串定位
"""
import struct
from capstone import Cs, CS_ARCH_X86, CS_MODE_32, CS_GRP_CALL, CS_GRP_JUMP

DLL = r"D:\Program Files (x86)\Steam\steamapps\common\Total War SHOGUN 2\Empire.Retail.dll"
IMAGE_BASE = 0x10000000


class PE:
    def __init__(self, path=DLL):
        with open(path, "rb") as f:
            self.data = f.read()
        self.path = path
        e_lfanew = struct.unpack_from("<I", self.data, 0x3C)[0]
        self.e_lfanew = e_lfanew
        nsec = struct.unpack_from("<H", self.data, e_lfanew + 6)[0]
        opt_size = struct.unpack_from("<H", self.data, e_lfanew + 20)[0]
        magic = struct.unpack_from("<H", self.data, e_lfanew + 24)[0]
        self.is_pe32 = (magic == 0x10B)
        # PE32: ImageBase 在 OptionalHeader+0x1C（4 字节）；PE32+: OptionalHeader+0x18（8 字节）
        if self.is_pe32:
            self.image_base = struct.unpack_from("<I", self.data, e_lfanew + 24 + 0x1C)[0]
        else:
            self.image_base = struct.unpack_from("<Q", self.data, e_lfanew + 24 + 0x18)[0]
        sec = e_lfanew + 24 + opt_size
        self.sections = []  # (name, va, rptr, rsize, vsize)
        for i in range(nsec):
            off = sec + i * 40
            name = self.data[off:off + 8].rstrip(b"\0").decode("ascii", "replace")
            vsize, va, rsize, rptr = struct.unpack_from("<IIII", self.data, off + 8)
            self.sections.append((name, self.image_base + va, rptr, rsize, vsize))
        # 数据目录（PE32: 数据目录在 opt header 0x60 处，16 项，每项 8 字节）
        dd = e_lfanew + 24 + 0x60
        self.imports_rva = struct.unpack_from("<I", self.data, dd + 8)[0]
        self.exports_rva = struct.unpack_from("<I", self.data, dd)[0]
        self.md = Cs(CS_ARCH_X86, CS_MODE_32)
        self.md.detail = True

    # ---------- 地址转换 ----------
    def rva_to_off(self, rva):
        for name, va, rptr, rsize, vsize in self.sections:
            if va <= self.image_base + rva < va + vsize:
                return rptr + (rva - (va - self.image_base))
        return None

    def off_to_rva(self, off):
        for name, va, rptr, rsize, vsize in self.sections:
            if rptr <= off < rptr + rsize:
                return (off - rptr) + (va - self.image_base)
        return None

    def va(self, rva):
        return self.image_base + rva

    def sec_of_rva(self, rva):
        for name, va, rptr, rsize, vsize in self.sections:
            if va <= self.image_base + rva < va + vsize:
                return name
        return "?"

    # ---------- 字节/字符串 ----------
    def find(self, pat, start=0):
        return self.data.find(pat, start)

    def find_str(self, s, start=0):
        return self.find(s.encode("utf-8"), start)

    def read_str_at_rva(self, rva, maxlen=256):
        off = self.rva_to_off(rva)
        if off is None:
            return None
        end = self.data.find(b"\0", off, off + maxlen)
        if end < 0:
            end = off + maxlen
        return self.data[off:end].decode("ascii", "replace")

    # ---------- 反汇编 ----------
    def disasm(self, rva, size=0x100, ctx_before=0):
        """反汇编从 rva 开始（可带前文）。返回 [(rva, 'mnemonic op_str', is_call/jmp)]"""
        off = self.rva_to_off(rva)
        if off is None:
            return []
        start = off - ctx_before if off - ctx_before >= 0 else 0
        chunk = self.data[start:off + size]
        base = rva - ctx_before
        out = []
        for insn in self.md.disasm(chunk, base):
            g = set(insn.groups)
            tag = ""
            if CS_GRP_CALL in g:
                tag = "CALL"
            elif CS_GRP_JUMP in g:
                tag = "JMP"
            out.append((insn.address, insn.mnemonic, insn.op_str, tag))
        return out

    def show(self, rva, size=0x100, ctx_before=0, mark=None, start_line=0):
        lines = self.disasm(rva, size, ctx_before)
        for i, (a, m, o, tag) in enumerate(lines):
            if i < start_line:
                continue
            star = " >>>" if mark is not None and a == mark else "    "
            t = f" [{tag}]" if tag else ""
            print(f"{star} 0x{a:08x}  {m} {o}{t}")

    # ---------- xref 扫描 ----------
    def _iter_imm32(self, target_rva):
        """全文件扫 imm32 == target_rva（低精度，含数据段误报），yield 文件偏移"""
        pat = struct.pack("<I", target_rva)
        pos = 0
        while True:
            i = self.data.find(pat, pos)
            if i < 0:
                return
            yield i
            pos = i + 1

    def xref_code(self, target_rva, maxhits=40):
        """扫 .text 中直接引用 target_rva 的指令：push imm32 / mov reg,imm32 /
        lea reg,[imm32] / cmp 类。返回 [(insn_rva, mnemonic, op_str)]。
        注意：指令中的 imm32 是 VA（image_base + rva）。"""
        text = [s for s in self.sections if s[0] == ".text"][0]
        t_off, t_rva, t_size = text[2], text[1] - self.image_base, text[4]
        pat = struct.pack("<I", self.image_base + target_rva)
        hits = []
        pos = 0
        while True:
            i = self.data.find(pat, pos)
            if i < 0:
                break
            if t_off < i < t_off + t_size:
                i_rva = (i - t_off) + t_rva
                # 从 [i-7, i] 窗口中尝试每个起始偏移，找恰好包含 imm32 的指令
                for insn_off in range(i - 7, i + 1):
                    if insn_off < t_off or insn_off > i:
                        continue
                    rva = (insn_off - t_off) + t_rva
                    insns = list(self.md.disasm(
                        self.data[insn_off:i + 5], rva))
                    if not insns:
                        continue
                    first = insns[0]
                    if first.address + first.size < i_rva + 4:
                        continue  # 指令结束前没盖住 imm32
                    if first.size >= 5 and first.address + first.size <= i_rva + 5 + 2:
                        hits.append((first.address, first.mnemonic, first.op_str))
                        break
            pos = i + 1
            if len(hits) >= maxhits:
                break
        # 去重保序
        seen, out = set(), []
        for h in hits:
            if h[0] not in seen:
                seen.add(h[0])
                out.append(h)
        return out

    def callers_of(self, target_rva, maxhits=100):
        """扫 .text 中 E8 rel32 相对 call 指向 target_rva 的位置"""
        text = [s for s in self.sections if s[0] == ".text"][0]
        t_off, t_va, t_size = text[2], text[1] - self.image_base, text[4]
        out = []
        for off in range(t_off, t_off + t_size - 5):
            if self.data[off] == 0xE8:
                rel = struct.unpack_from("<i", self.data, off + 1)[0]
                src_rva = (off - t_off) + t_va
                dst = (src_rva + 5 + rel) & 0xFFFFFFFF
                if dst == target_rva:
                    out.append(src_rva)
                    if len(out) >= maxhits:
                        break
        return out

    def refs_to_str(self, s, maxhits=40):
        """定位字符串并返回其 RVA + .text 引用（带反汇编上下文行）"""
        off = self.find_str(s)
        if off is None:
            return None, []
        rva = self.off_to_rva(off)
        refs = self.xref_code(rva, maxhits)
        return rva, refs

    # ---------- 导入表 ----------
    def imports(self):
        """返回 [(dll_name, [(name, iat_rva)])]"""
        if not self.imports_rva:
            return []
        out = []
        dd_rva = self.imports_rva
        idx = 0
        while True:
            off = self.rva_to_off(dd_rva + idx * 20)
            if off is None:
                break
            oft, ts, fwd, name_rva, iat = struct.unpack_from("<IIIII", self.data, off)
            if all(v == 0 for v in (oft, ts, fwd, name_rva, iat)):
                break
            dll_name = self.read_str_at_rva(name_rva, 128) or "?"
            funcs = []
            if oft:
                thunk = oft
            elif iat:
                thunk = iat
            else:
                thunk = 0
            while thunk:
                toff = self.rva_to_off(thunk)
                if toff is None:
                    break
                val = struct.unpack_from("<I", self.data, toff)[0]
                if val == 0:
                    break
                if val & 0x80000000:
                    funcs.append((f"ord_{val & 0xFFFF}", thunk))
                else:
                    hint = self.read_str_at_rva(val + 2, 128) or "?"
                    funcs.append((hint, thunk))
                thunk += 4
            out.append((dll_name, funcs))
            idx += 1
            if idx > 200:
                break
        return out


if __name__ == "__main__":
    pe = PE()
    print(f"DLL: {pe.path}  size={len(pe.data)}  is_pe32={pe.is_pe32}  image_base={pe.image_base:#x}")
    print("sections:")
    for name, va, rptr, rsize, vsize in pe.sections:
        print(f"  {name:8s} VA={va:#x} RVA={va-pe.image_base:#x} raw={rptr:#x} rawsz={rsize:#x} vsz={vsize:#x}")
    print("imports:")
    for dll, funcs in pe.imports():
        print(f"  {dll}: {len(funcs)} funcs")
        for name, _ in funcs[:20]:
            print(f"      {name}")
