#!/usr/bin/env python3
#
# Reverse : Generate an indented asm code (pseudo-C) with colored syntax.
# Copyright (C) 2015    Joel
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.    See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.    If not, see <http://www.gnu.org/licenses/>.
#

import time
import struct

from lib.graph import Graph
from lib.utils import debug__, BYTES_PRINTABLE_SET, get_char
from lib.fileformat.binary import Binary, T_BIN_PE
from lib.output import print_no_end
from lib.colors import (pick_color, color_addr, color_symbol,
        color_section, color_string, color_comment)
from lib.exceptions import ExcSymNotFound, ExcArch, ExcNotAddr, ExcNotExec


class Jmptable():
    def __init__(self, inst_addr, table_addr, table, name):
        self.inst_addr = inst_addr
        self.table_addr = table_addr
        self.table = table
        self.name = name


class Disassembler():
    def __init__(self, filename, raw_type, raw_base,
                 raw_big_endian, sym, rev_sym,
                 jmptables, inline_comments,
                 previous_comments, load_symbols=True):
        import capstone as CAPSTONE

        self.code = {}
        self.binary = Binary(filename, raw_type, raw_base, raw_big_endian)

        arch, mode = self.binary.get_arch()

        if arch is None or mode is None:
            raise ExcArch(self.binary.get_arch_string())

        if load_symbols:
            self.binary.load_symbols()
        else:
            self.binary.symbols = sym
            self.binary.reverse_symbols = rev_sym

        self.binary.load_section_names()

        self.binary.load_data_sections()

        self.capstone = CAPSTONE
        self.md = CAPSTONE.Cs(arch, mode)
        self.md.detail = True
        self.arch = arch
        self.mode = mode
        self.jmptables = jmptables
        self.inline_comments = inline_comments
        self.previous_comments = previous_comments


    def get_unpack_str(self, size_word):
        if self.mode & self.capstone.CS_MODE_BIG_ENDIAN:
            endian = ">"
        else:
            endian = "<"
        if size_word == 1:
            unpack_str = endian + "B"
        elif size_word == 2:
            unpack_str = endian + "H"
        elif size_word == 4:
            unpack_str = endian + "L"
        elif size_word == 8:
            unpack_str = endian + "Q"
        return unpack_str


    def add_symbol(self, addr, name):
        if name in self.binary.symbols:
            last = self.binary.symbols[name]
            del self.binary.reverse_symbols[last]

        self.binary.symbols[name] = addr
        self.binary.reverse_symbols[addr] = name

        return name


    def read_array(self, ad, array_max_size, size_word):
        unpack_str = self.get_unpack_str(size_word)
        N = size_word * array_max_size
        s_name, s_start, s_end = self.binary.get_section_meta(ad)
        array = []
        l = 0

        while l < array_max_size:
            buf = self.binary.section_stream_read(ad, N)
            if not buf:
                break

            i = 0
            while i < len(buf):
                b = buf[i:i + size_word]

                if ad > s_end or len(b) != size_word:
                    return array

                w = struct.unpack(unpack_str, b)[0]
                array.append(w)

                ad += size_word
                i += size_word
                l += 1
                if l >= array_max_size:
                    return array
        return array


    def check_addr(self, ctx, addr):
        addr_exists, is_exec = self.binary.check_addr(addr)
        if not ctx.print_data and not is_exec:
            raise ExcNotExec(addr)
        if not addr_exists:
            raise ExcNotAddr(addr)


    def load_arch_module(self):
        if self.arch == self.capstone.CS_ARCH_X86:
            import lib.arch.x86 as ARCH
        elif self.arch == self.capstone.CS_ARCH_ARM:
            import lib.arch.arm as ARCH
        elif self.arch == self.capstone.CS_ARCH_MIPS:
            import lib.arch.mips as ARCH
        else:
            raise NotImplementedError
        return ARCH


    def get_addr_from_string(self, opt_addr, raw=False):
        if opt_addr is None:
            if raw:
                return 0
            search = ["main", "_main"]
        else:
            search = [opt_addr]

        for s in search:
            if s.startswith("0x"):
                a = int(opt_addr, 16)
            else:
                a = self.binary.symbols.get(s, -1)
                if a == -1:
                    a = self.binary.section_names.get(s, -1)

            if a != -1:
                return a

        raise ExcSymNotFound(search[0])


    def print_section_meta(self, name, start, end):
        print_no_end(color_section(name.ljust(20)))
        print_no_end(" [ ")
        print_no_end(hex(start))
        print_no_end(" - ")
        print_no_end(hex(end))
        print_no_end(" - %d" % (end - start + 1))
        print(" ]")


    def dump_asm(self, ctx, lines):
        from capstone import CS_OP_IMM
        ARCH = self.load_arch_module()
        ARCH_UTILS = ARCH.utils
        ARCH_OUTPUT = ARCH.output

        s_name, s_start, s_end = self.binary.get_section_meta(ctx.entry_addr)
        self.print_section_meta(s_name, s_start, s_end)

        # WARNING: this assume that on every architectures the jump
        # address is the last operand (operands[-1])

        # set jumps color
        ad = ctx.entry_addr
        l = 0
        while l < lines and ad < s_end:
            i = self.lazy_disasm(ad, s_start)
            if i is None:
                ad += 1
            else:
                if ARCH_UTILS.is_jump(i) and i.operands[-1].type == CS_OP_IMM:
                    pick_color(i.operands[-1].value.imm)
                ad += i.size
            l += 1

        # Here we have loaded all instructions we want to print
        if self.binary.type == T_BIN_PE:
            self.binary.pe_reverse_stripped_symbols(self)

        o = ARCH_OUTPUT.Output(ctx)

        # dump
        ad = ctx.entry_addr
        l = 0
        while l < lines and ad < s_end:
            i = self.lazy_disasm(ad, s_start)
            if i is None:
                ad += 1
                o.print_bad(ad)
            else:
                o.print_inst(i)
                ad += i.size
            l += 1


    def dump_data_ascii(self, ctx, lines):
        N = 128 # read by block of 128 bytes
        addr = ctx.entry_addr

        s_name, s_start, s_end = self.binary.get_section_meta(ctx.entry_addr)
        self.print_section_meta(s_name, s_start, s_end)

        l = 0
        ascii_str = []
        addr_str = -1

        while l < lines:
            buf = self.binary.section_stream_read(addr, N)
            if not buf:
                break

            i = 0
            while i < len(buf):

                if addr > s_end:
                    return

                j = i
                while j < len(buf):
                    c = buf[j]
                    if c not in BYTES_PRINTABLE_SET:
                        break
                    if addr_str == -1:
                        addr_str = addr
                    ascii_str.append(c)
                    j += 1

                if c != 0 and j == len(buf):
                    addr += j - i
                    break

                if c == 0 and len(ascii_str) >= 2:
                    print_no_end(color_addr(addr_str))
                    print_no_end(color_string(
                            "\"" + "".join(map(get_char, ascii_str)) + "\""))
                    print(", 0")
                    addr += j - i
                    i = j
                else:
                    print_no_end(color_addr(addr))
                    print("0x%.2x " % buf[i])
                    addr += 1
                    i += 1

                addr_str = -1
                ascii_str = []
                l += 1
                if l >= lines:
                    return


    def dump_data(self, ctx, lines, size_word):
        s_name, s_start, s_end = self.binary.get_section_meta(ctx.entry_addr)
        self.print_section_meta(s_name, s_start, s_end)

        ad = ctx.entry_addr

        for w in self.read_array(ctx.entry_addr, lines, size_word):
            if ad in self.binary.reverse_symbols:
                print(color_symbol(self.binary.reverse_symbols[ad]))
            print_no_end(color_addr(ad))
            print_no_end("0x%.2x" % w)
            sec_name, is_data = self.binary.is_address(w)
            if sec_name is not None:
                print_no_end(" (")
                print_no_end(color_section(sec_name))
                print_no_end(")")
                if size_word >= 4 and w in self.binary.reverse_symbols:
                    print_no_end(" ")
                    print_no_end(color_symbol(self.binary.reverse_symbols[w]))
            ad += size_word
            print()


    def print_calls(self, ctx):
        ARCH = self.load_arch_module()
        ARCH_UTILS = ARCH.utils
        ARCH_OUTPUT = ARCH.output

        s_name, s_start, s_end = self.binary.get_section_meta(ctx.entry_addr)
        self.print_section_meta(s_name, s_start, s_end)
        o = ARCH_OUTPUT.Output(ctx)

        ad = s_start
        while ad < s_end:
            i = self.lazy_disasm(ad, s_start)
            if i is None:
                ad += 1
            else:
                ad += i.size
                if ARCH_UTILS.is_call(i):
                    o.print_inst(i)


    def print_symbols(self, print_sections, sym_filter=None):
        if sym_filter is not None:
            sym_filter = sym_filter.lower()

        for sy in self.binary.symbols:
            addr = self.binary.symbols[sy]
            if sym_filter is None or sym_filter in sy.lower():
                sec_name, _ = self.binary.is_address(addr)
                if sy:
                    print_no_end(color_addr(addr) + " " + sy)
                    if print_sections and sec_name is not None:
                        print_no_end(" (" + color_section(sec_name) + ")")
                    print()


    def lazy_disasm(self, addr, stay_in_section=-1):
        meta  = self.binary.get_section_meta(addr)
        if meta is None:
            return None

        _, start, _ = meta

        if stay_in_section != -1 and start != stay_in_section:
            return None

        if addr in self.code:
            return self.code[addr]
        
        # Disassemble by block of N bytes
        N = 1024
        d = self.binary.section_stream_read(addr, N)
        gen = self.md.disasm(d, addr)

        try:
            first = next(gen)
        except StopIteration:
            return None

        for i in gen:
            if i.address in self.code:
                break
            self.code[i.address] = i

        return first


    def __prefetch_inst(self, inst):
        return self.lazy_disasm(inst.address + inst.size)


    # Generate a flow graph of the given function (addr)
    def get_graph(self, entry_addr):
        from capstone import CS_OP_IMM, CS_ARCH_MIPS, CS_OP_REG

        ARCH_UTILS = self.load_arch_module().utils

        gph = Graph(self, entry_addr)
        stack = [entry_addr]
        start = time.clock()
        prefetch = None

        # WARNING: this assume that on every architectures the jump
        # address is the last operand (operands[-1])

        while stack:
            ad = stack.pop()
            inst = self.lazy_disasm(ad)

            if inst is None:
                # Remove all previous instructions which have a link
                # to this instruction.
                if ad in gph.link_in:
                    for i in gph.link_in[ad]:
                        gph.link_out[i].remove(ad)
                    for i in gph.link_in[ad]:
                        if not gph.link_out[i]:
                            del gph.link_out[i]
                    del gph.link_in[ad]
                continue

            if gph.exists(inst):
                continue

            if ARCH_UTILS.is_ret(inst):
                if self.arch == CS_ARCH_MIPS:
                    prefetch = self.__prefetch_inst(inst)
                gph.add_node(inst, prefetch)

            elif ARCH_UTILS.is_uncond_jump(inst):
                if self.arch == CS_ARCH_MIPS:
                    prefetch = self.__prefetch_inst(inst)
                gph.uncond_jumps_set.add(ad)
                op = inst.operands[-1]
                if op.type == CS_OP_IMM:
                    nxt = op.value.imm
                    stack.append(nxt)
                    gph.set_next(inst, nxt, prefetch)
                else:
                    if inst.address in self.jmptables:
                        table = self.jmptables[inst.address].table
                        gph.set_jmptable_next(inst, table, prefetch)
                        for ad in table:
                            stack.append(ad)
                    else:
                        # Can't interpret jmp ADDR|reg
                        gph.add_node(inst, prefetch)

            elif ARCH_UTILS.is_cond_jump(inst):
                if self.arch == CS_ARCH_MIPS:
                    prefetch = self.__prefetch_inst(inst)
                gph.cond_jumps_set.add(ad)
                op = inst.operands[-1]
                if op.type == CS_OP_IMM:
                    if self.arch == CS_ARCH_MIPS:
                        direct_nxt = prefetch.address + prefetch.size
                    else:
                        direct_nxt = inst.address + inst.size

                    nxt_jmp = op.value.imm

                    stack.append(direct_nxt)
                    stack.append(nxt_jmp)
                    gph.set_cond_next(inst, nxt_jmp, direct_nxt, prefetch)
                else:
                    # Can't interpret jmp ADDR|reg
                    gph.add_node(inst, prefetch)

            else:
                nxt = inst.address + inst.size
                stack.append(nxt)
                gph.set_next(inst, nxt)

        if len(gph.nodes) == 0:
            return None

        if self.binary.type == T_BIN_PE:
            self.binary.pe_reverse_stripped_symbols(self)

        elapsed = time.clock()
        elapsed = elapsed - start
        debug__("Graph built in %fs" % elapsed)

        return gph


    def add_jmptable(self, inst_addr, table_addr, entry_size, nb_entries):
        name = self.add_symbol(table_addr, "jmptable_0x%x" % table_addr)

        table = self.read_array(table_addr, nb_entries, entry_size)
        self.jmptables[inst_addr] = Jmptable(inst_addr, table_addr, table, name)

        self.inline_comments[inst_addr] = "switch statement %s" % name

        all_cases = {}
        for ad in table:
            all_cases[ad] = []

        case = 0
        for ad in table:
            all_cases[ad].append(case)
            case += 1

        for ad in all_cases:
            self.previous_comments[ad] = \
                ["case %s  %s" % (
                    ", ".join(map(str, all_cases[ad])),
                    name
                )]
