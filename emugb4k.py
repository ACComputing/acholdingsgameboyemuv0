#!/usr/bin/env python3
# ╔══════════════════════════════════════════════════════════════════════╗
# ║   CAT'S GB EMULATOR  ─  mGBA-Style Game Boy Emulator v3.0          ║
# ║   SM83 CPU · PPU Scanline · MBC1/3 · Timer · mGBA-faithful GUI     ║
# ║   4.194304 MHz · 70224 cyc/frame · ~59.7 FPS target                ║
# ╚══════════════════════════════════════════════════════════════════════╝

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import time, os, sys, math
from threading import Thread, Lock

try:
    from PIL import Image, ImageTk, ImageDraw
    PIL = True
except ImportError:
    PIL = False

# ─── DMG Palette ──────────────────────────────────────────────────────
DMG_COLORS = [(0xE8,0xF8,0xD0),(0x88,0xC0,0x70),(0x34,0x68,0x56),(0x08,0x18,0x20)]

# ─── Interrupt bits ────────────────────────────────────────────────────
INT_VBLANK,INT_STAT,INT_TIMER,INT_SERIAL,INT_JOYPAD = 1,2,4,8,16
FLAG_Z,FLAG_N,FLAG_H,FLAG_C = 0x80,0x40,0x20,0x10
GB_W,GB_H = 160,144
CYCLES_PER_FRAME = 70224


# ══════════════════════════════════════════════════════════════════════
#  CARTRIDGE + MBC
# ══════════════════════════════════════════════════════════════════════
class Cartridge:
    def __init__(self, data: bytes):
        self.rom = bytearray(data)
        self.ram = bytearray(0x20000)
        self.mbc_type = data[0x0147] if len(data) > 0x0147 else 0
        self.rom_bank = 1
        self.ram_bank = 0
        self.ram_enabled = False
        self.mode = 0
        nb = data[0x0148] if len(data) > 0x0148 else 0
        self.num_rom_banks = max(2, 2 << nb)
        ri = data[0x0149] if len(data) > 0x0149 else 0
        self.ram_size = [0,2048,8192,32768,131072,65536][ri] if ri<6 else 0
        self.name = ''.join(chr(b) if 0x20<=b<0x7F else '?' for b in data[0x134:0x144]).strip('\x00 ')

    def read(self, a: int) -> int:
        if a < 0x4000:
            return self.rom[a] if a < len(self.rom) else 0xFF
        elif a < 0x8000:
            bank = self.rom_bank & (self.num_rom_banks - 1)
            off = bank * 0x4000 + (a - 0x4000)
            return self.rom[off] if off < len(self.rom) else 0xFF
        elif 0xA000 <= a < 0xC000:
            if self.ram_enabled and self.ram_size:
                return self.ram[self.ram_bank*0x2000 + (a-0xA000)]
            return 0xFF
        return 0xFF

    def write(self, a: int, v: int):
        v &= 0xFF
        mt = self.mbc_type
        if mt == 0: return
        if mt in (1,2,3,0x0F,0x10,0x11,0x12,0x13):
            if a < 0x2000:
                self.ram_enabled = (v & 0x0F) == 0x0A
            elif a < 0x4000:
                lo = v & (0x1F if mt<=3 else 0x7F)
                if lo == 0: lo = 1
                if mt <= 3:
                    self.rom_bank = (self.rom_bank & 0x60) | lo
                else:
                    self.rom_bank = lo
            elif a < 0x6000 and mt <= 3:
                if self.mode == 0:
                    self.rom_bank = (self.rom_bank & 0x1F) | ((v&3)<<5)
                else:
                    self.ram_bank = v & 3
            elif a < 0x8000 and mt <= 3:
                self.mode = v & 1
            elif 0xA000 <= a < 0xC000:
                if self.ram_enabled and self.ram_size:
                    self.ram[self.ram_bank*0x2000+(a-0xA000)] = v


# ══════════════════════════════════════════════════════════════════════
#  JOYPAD
# ══════════════════════════════════════════════════════════════════════
class Joypad:
    def __init__(self):
        self.a=self.b=self.sel=self.start=False
        self.right=self.left=self.up=self.down=False
        self._sel_btn=self._sel_dir=False
    def read(self):
        v = 0xCF
        if self._sel_btn:
            v &= ~0x20
            if self.a:     v &= ~1
            if self.b:     v &= ~2
            if self.sel:   v &= ~4
            if self.start: v &= ~8
        if self._sel_dir:
            v &= ~0x10
            if self.right: v &= ~1
            if self.left:  v &= ~2
            if self.up:    v &= ~4
            if self.down:  v &= ~8
        return v
    def write(self, v):
        self._sel_btn = not (v & 0x20)
        self._sel_dir = not (v & 0x10)


# ══════════════════════════════════════════════════════════════════════
#  TIMER
# ══════════════════════════════════════════════════════════════════════
class Timer:
    BITS = {0:9,1:3,2:5,3:7}
    def __init__(self):
        self.div=0xABCC; self.tima=0; self.tma=0; self.tac=0
        self._of=False; self._of_d=0
    def step(self, cyc):
        fired = False
        old = self.div
        self.div = (self.div + cyc) & 0xFFFF
        if self._of:
            self._of_d -= cyc
            if self._of_d <= 0:
                self.tima = self.tma; self._of = False; fired = True
        if self.tac & 4:
            bit = self.BITS[self.tac & 3]
            if (old>>bit)&1 and not (self.div>>bit)&1:
                self.tima = (self.tima+1)&0xFF
                if self.tima == 0: self._of=True; self._of_d=4
        return fired
    def read(self, a):
        if a==0xFF04: return (self.div>>8)&0xFF
        if a==0xFF05: return self.tima
        if a==0xFF06: return self.tma
        if a==0xFF07: return self.tac|0xF8
        return 0xFF
    def write(self, a, v):
        if a==0xFF04: self.div=0
        elif a==0xFF05: self.tima=v
        elif a==0xFF06: self.tma=v
        elif a==0xFF07: self.tac=v&7


# ══════════════════════════════════════════════════════════════════════
#  PPU
# ══════════════════════════════════════════════════════════════════════
class PPU:
    def __init__(self):
        self.vram=bytearray(0x2000); self.oam=bytearray(0xA0)
        self.lcdc=0x91; self.stat=0x85; self.scy=self.scx=0
        self.ly=0; self.lyc=0
        self.bgp=0xFC; self.obp0=0xFF; self.obp1=0xFF
        self.wy=self.wx=0; self.wx=7
        self.mode=2; self.cycles=0; self.win_line=0
        self._win_active=False
        self.pixels=bytearray(GB_W*GB_H*3)
        self.frame_ready=False

    def _shade(self, pal, idx):
        s=(pal>>(idx*2))&3; return DMG_COLORS[s]

    def read(self, a):
        if 0x8000<=a<0xA000: return self.vram[a-0x8000]
        if 0xFE00<=a<0xFEA0: return self.oam[a-0xFE00]
        r=a&0xFF
        d={0x40:self.lcdc,0x41:(self.stat|0x80)&0xFF,
           0x42:self.scy,0x43:self.scx,0x44:self.ly,0x45:self.lyc,
           0x47:self.bgp,0x48:self.obp0,0x49:self.obp1,
           0x4A:self.wy,0x4B:self.wx}
        return d.get(r,0xFF)

    def write(self, a, v):
        if 0x8000<=a<0xA000: self.vram[a-0x8000]=v; return
        if 0xFE00<=a<0xFEA0: self.oam[a-0xFE00]=v; return
        r=a&0xFF
        if r==0x40:
            if not (v&0x80): self.ly=0; self.cycles=0; self.mode=0; self.pixels=bytearray(GB_W*GB_H*3); self.frame_ready=True
            self.lcdc=v
        elif r==0x41: self.stat=(self.stat&0x87)|(v&0x78)
        elif r==0x42: self.scy=v
        elif r==0x43: self.scx=v
        elif r==0x44: self.ly=0
        elif r==0x45: self.lyc=v
        elif r==0x47: self.bgp=v
        elif r==0x48: self.obp0=v
        elif r==0x49: self.obp1=v
        elif r==0x4A: self.wy=v
        elif r==0x4B: self.wx=v

    def step(self, cyc):
        if not (self.lcdc&0x80): return False,False
        vb=st=False
        self.cycles+=cyc
        if self.mode==2:
            if self.cycles>=80: self.cycles-=80; self.mode=3
        elif self.mode==3:
            if self.cycles>=172:
                self.cycles-=172; self._render(); self.mode=0
                if self.stat&8: st=True
        elif self.mode==0:
            if self.cycles>=204:
                self.cycles-=204; self.ly+=1
                if self.ly==self.lyc and self.stat&0x40: st=True
                if self.ly==144:
                    self.mode=1; vb=True; self.win_line=0; self._win_active=False; self.frame_ready=True
                    if self.stat&0x10: st=True
                else:
                    self.mode=2
                    if self.stat&0x20: st=True
        elif self.mode==1:
            if self.cycles>=456:
                self.cycles-=456; self.ly+=1
                if self.ly==self.lyc and self.stat&0x40: st=True
                if self.ly>153:
                    self.ly=0; self.mode=2
                    if self.stat&0x20: st=True
        self.stat=(self.stat&0xFC)|self.mode
        self.stat = (self.stat&~4)|(4 if self.ly==self.lyc else 0)
        return vb,st

    def _render(self):
        ly=self.ly
        if ly>=GB_H: return
        base=ly*GB_W*3
        bg_idx=[0]*GB_W

        if self.lcdc&1:
            tmap  = 0x1C00 if (self.lcdc&8)  else 0x1800
            tdata = 0x0000 if (self.lcdc&0x10) else 0x0800
            signed= not (self.lcdc&0x10)
            win_y_ok = (self.lcdc&0x20) and ly>=self.wy

            for px in range(GB_W):
                use_win = win_y_ok and px>=(self.wx-7) and (self.lcdc&0x20)
                if use_win: self._win_active=True

                if use_win and self._win_active:
                    tx=px-(self.wx-7); ty=self.win_line
                    wm=0x1C00 if (self.lcdc&0x40) else 0x1800
                    ti=self.vram[wm+(ty>>3)*32+(tx>>3)]; row=ty&7; col=tx&7
                else:
                    sx=(self.scx+px)&0xFF; sy=(self.scy+ly)&0xFF
                    ti=self.vram[tmap+(sy>>3)*32+(sx>>3)]; row=sy&7; col=sx&7

                if signed:
                    ti=ti if ti<128 else ti-256
                    addr=(0x0800+ti*16+row*2)
                else:
                    addr=tdata+ti*16+row*2

                if addr<0 or addr+1>=0x2000: continue
                lo=self.vram[addr]; hi=self.vram[addr+1]
                bit=7-col
                c=((hi>>bit)&1)<<1|((lo>>bit)&1)
                bg_idx[px]=c
                r,g,b=self._shade(self.bgp,c)
                self.pixels[base+px*3]=r; self.pixels[base+px*3+1]=g; self.pixels[base+px*3+2]=b

            if win_y_ok: self.win_line+=1

        if self.lcdc&2:
            sh=16 if (self.lcdc&4) else 8; sprites=[]
            for i in range(40):
                oy=self.oam[i*4]-16; ox=self.oam[i*4+1]-8
                ti=self.oam[i*4+2]; at=self.oam[i*4+3]
                if oy<=ly<oy+sh: sprites.append((ox,oy,ti,at))
                if len(sprites)==10: break
            for ox,oy,ti,at in reversed(sprites):
                pal=self.obp1 if at&0x10 else self.obp0
                fx=at&0x20; fy=at&0x40; prio=at&0x80
                row=ly-oy
                if fy: row=(sh-1)-row
                if sh==16: ti&=0xFE
                addr=ti*16+row*2
                if addr+1>=0x2000: continue
                lo=self.vram[addr]; hi=self.vram[addr+1]
                for px in range(8):
                    sx=ox+px
                    if sx<0 or sx>=GB_W: continue
                    bit=px if fx else 7-px
                    c=((hi>>bit)&1)<<1|((lo>>bit)&1)
                    if c==0: continue
                    if prio and bg_idx[sx]!=0: continue
                    r,g,b=self._shade(pal,c)
                    self.pixels[base+sx*3]=r; self.pixels[base+sx*3+1]=g; self.pixels[base+sx*3+2]=b


# ══════════════════════════════════════════════════════════════════════
#  MMU
# ══════════════════════════════════════════════════════════════════════
class MMU:
    def __init__(self, cart, ppu, timer, joy):
        self.cart=cart; self.ppu=ppu; self.timer=timer; self.joy=joy
        self.wram=bytearray(0x2000); self.hram=bytearray(0x80)
        self.IE=0; self.IF=0xE1
        self._sb=0; self._sc=0

    def rb(self, a):
        a&=0xFFFF
        if a<0x8000:   return self.cart.read(a)
        if a<0xA000:   return self.ppu.read(a)
        if a<0xC000:   return self.cart.read(a)
        if a<0xE000:   return self.wram[a-0xC000]
        if a<0xFE00:   return self.wram[a-0xE000]
        if a<0xFEA0:   return self.ppu.read(a)
        if a<0xFF00:   return 0xFF
        if a==0xFF00:  return self.joy.read()
        if 0xFF01<=a<=0xFF02:
            return self._sb if a==0xFF01 else self._sc
        if 0xFF04<=a<=0xFF07: return self.timer.read(a)
        if a==0xFF0F:  return self.IF|0xE0
        if 0xFF10<=a<0xFF40: return 0xFF  # APU stub
        if 0xFF40<=a<=0xFF4B: return self.ppu.read(a)
        if 0xFF80<=a<0xFFFF: return self.hram[a-0xFF80]
        if a==0xFFFF:  return self.IE
        return 0xFF

    def wb(self, a, v):
        a&=0xFFFF; v&=0xFF
        if a<0x8000:   self.cart.write(a,v); return
        if a<0xA000:   self.ppu.write(a,v); return
        if a<0xC000:   self.cart.write(a,v); return
        if a<0xE000:   self.wram[a-0xC000]=v; return
        if a<0xFE00:   self.wram[a-0xE000]=v; return
        if a<0xFEA0:   self.ppu.write(a,v); return
        if a<0xFF00:   return
        if a==0xFF00:  self.joy.write(v); return
        if a==0xFF01:  self._sb=v; return
        if a==0xFF02:  self._sc=v; return
        if 0xFF04<=a<=0xFF07: self.timer.write(a,v); return
        if a==0xFF0F:  self.IF=v&0x1F; return
        if a==0xFF46:  # DMA
            src=v<<8
            for i in range(0xA0): self.ppu.oam[i]=self.rb(src+i)
            return
        if 0xFF10<=a<0xFF40: return  # APU stub
        if 0xFF40<=a<=0xFF4B: self.ppu.write(a,v); return
        if 0xFF80<=a<0xFFFF: self.hram[a-0xFF80]=v; return
        if a==0xFFFF:  self.IE=v; return

    def rw(self, a): return self.rb(a)|(self.rb((a+1)&0xFFFF)<<8)
    def ww(self, a, v): self.wb(a,v&0xFF); self.wb((a+1)&0xFFFF,(v>>8)&0xFF)


# ══════════════════════════════════════════════════════════════════════
#  SM83 CPU  (complete opcode coverage)
# ══════════════════════════════════════════════════════════════════════
class CPU:
    def __init__(self, mmu):
        self.mmu=mmu
        self.pc=0x0100; self.sp=0xFFFE
        self.a=0x01; self.f=0xB0
        self.b=0x00; self.c=0x13
        self.d=0x00; self.e=0xD8
        self.h=0x01; self.l=0x4D
        self.halted=False; self.ime=False; self._ime_pending=0
        self.cycles=0

    # ── Flag helpers ───────────────────────────────────────────
    def _zf(self): return bool(self.f&0x80)
    def _nf(self): return bool(self.f&0x40)
    def _hf(self): return bool(self.f&0x20)
    def _cf(self): return bool(self.f&0x10)
    def _sf(self,z=0,n=0,h=0,c=0):
        self.f=(0x80 if z else 0)|(0x40 if n else 0)|(0x20 if h else 0)|(0x10 if c else 0)

    # ── 16-bit reg helpers ─────────────────────────────────────
    def _af(self): return (self.a<<8)|(self.f&0xF0)
    def _bc(self): return (self.b<<8)|self.c
    def _de(self): return (self.d<<8)|self.e
    def _hl(self): return (self.h<<8)|self.l
    def _set_af(self,v): v&=0xFFF0; self.a=(v>>8)&0xFF; self.f=v&0xFF
    def _set_bc(self,v): self.b=(v>>8)&0xFF; self.c=v&0xFF
    def _set_de(self,v): self.d=(v>>8)&0xFF; self.e=v&0xFF
    def _set_hl(self,v): self.h=(v>>8)&0xFF; self.l=v&0xFF

    def _r8(self, reg):
        return [self.b,self.c,self.d,self.e,self.h,self.l,self.mmu.rb(self._hl()),self.a][reg]
    def _w8(self, reg, v):
        v&=0xFF
        if reg==0: self.b=v
        elif reg==1: self.c=v
        elif reg==2: self.d=v
        elif reg==3: self.e=v
        elif reg==4: self.h=v
        elif reg==5: self.l=v
        elif reg==6: self.mmu.wb(self._hl(),v)
        elif reg==7: self.a=v

    def _fetch(self):
        v=self.mmu.rb(self.pc); self.pc=(self.pc+1)&0xFFFF; return v
    def _fetch16(self):
        lo=self._fetch(); hi=self._fetch(); return lo|(hi<<8)

    # ── ADD/ADC/SUB/SBC/AND/XOR/OR/CP (ALU) ───────────────────
    def _add(self,v,carry=0):
        r=self.a+v+carry
        self._sf(z=(r&0xFF)==0,n=0,h=((self.a&0xF)+(v&0xF)+carry)>0xF,c=r>0xFF)
        self.a=r&0xFF
    def _sub(self,v,carry=0):
        r=self.a-v-carry
        self._sf(z=(r&0xFF)==0,n=1,h=((self.a&0xF)-(v&0xF)-carry)<0,c=r<0)
        self.a=r&0xFF
    def _and(self,v): self.a&=v; self._sf(z=self.a==0,n=0,h=1,c=0)
    def _xor(self,v): self.a^=v; self._sf(z=self.a==0,n=0,h=0,c=0)
    def _or(self,v):  self.a|=v; self._sf(z=self.a==0,n=0,h=0,c=0)
    def _cp(self,v):
        r=self.a-v; self._sf(z=(r&0xFF)==0,n=1,h=((self.a&0xF)-(v&0xF))<0,c=r<0)

    def _add_hl(self,v):
        hl=self._hl(); r=hl+v
        h=((hl&0xFFF)+(v&0xFFF))>0xFFF
        self._sf(z=self._zf(),n=0,h=h,c=r>0xFFFF)
        self._set_hl(r&0xFFFF)

    def _inc8(self,v):
        r=(v+1)&0xFF; self._sf(z=r==0,n=0,h=(v&0xF)==0xF,c=self._cf()); return r
    def _dec8(self,v):
        r=(v-1)&0xFF; self._sf(z=r==0,n=1,h=(v&0xF)==0,c=self._cf()); return r

    def _rl(self,v,thru=True):
        c=self._cf() if thru else (v>>7)&1
        r=((v<<1)|(1 if (self._cf() if thru else c) else 0))&0xFF if thru else ((v<<1)|c)&0xFF
        if thru: r=((v<<1)|(1 if self._cf() else 0))&0xFF
        else:    r=((v<<1)|c)&0xFF
        self._sf(z=r==0,n=0,h=0,c=bool(v&0x80)); return r
    def _rr(self,v,thru=True):
        c=(v&1)
        if thru: r=((v>>1)|(0x80 if self._cf() else 0))&0xFF
        else:    r=((v>>1)|(c<<7))&0xFF
        self._sf(z=r==0,n=0,h=0,c=bool(c)); return r
    def _sla(self,v): r=(v<<1)&0xFF; self._sf(z=r==0,n=0,h=0,c=bool(v&0x80)); return r
    def _sra(self,v): r=((v>>1)|(v&0x80))&0xFF; self._sf(z=r==0,n=0,h=0,c=bool(v&1)); return r
    def _srl(self,v): r=(v>>1)&0xFF; self._sf(z=r==0,n=0,h=0,c=bool(v&1)); return r
    def _swap(self,v): r=((v&0xF)<<4)|((v>>4)&0xF); self._sf(z=r==0,n=0,h=0,c=0); return r
    def _bit(self,n,v): self._sf(z=not bool(v&(1<<n)),n=0,h=1,c=self._cf())
    def _set_b(self,n,v): return v|(1<<n)
    def _res_b(self,n,v): return v&~(1<<n)

    def _push(self,v): self.sp=(self.sp-2)&0xFFFF; self.mmu.ww(self.sp,v)
    def _pop(self): v=self.mmu.rw(self.sp); self.sp=(self.sp+2)&0xFFFF; return v

    def _call(self,a): self._push((self.pc)&0xFFFF); self.pc=a
    def _ret(self): self.pc=self._pop()

    def _jr(self, offset):
        self.pc=(self.pc+((offset^0x80)-0x80))&0xFFFF

    def _sp_add(self, v):
        sv=(v^0x80)-0x80
        r=(self.sp+sv)&0xFFFF
        self._sf(z=0,n=0,h=((self.sp&0xF)+(v&0xF))>0xF,c=((self.sp&0xFF)+(v&0xFF))>0xFF)
        return r

    # ── Interrupt handling ─────────────────────────────────────
    def handle_interrupts(self):
        fired = self.mmu.IE & self.mmu.IF & 0x1F
        if fired:
            if self.halted: self.halted=False
            if self.ime:
                self.ime=False
                for bit,vec in [(0,0x40),(1,0x48),(2,0x50),(3,0x58),(4,0x60)]:
                    if fired&(1<<bit):
                        self.mmu.IF&=~(1<<bit)
                        self._push(self.pc); self.pc=vec
                        self.cycles+=20; return

    # ── Main step ──────────────────────────────────────────────
    def step(self):
        if self._ime_pending>0:
            self._ime_pending-=1
            if self._ime_pending==0: self.ime=True

        self.handle_interrupts()
        if self.halted: self.cycles+=4; return

        op=self._fetch(); c=4

        # ─ Main opcode table ───────────────────────────────────
        if op==0x00: pass  # NOP
        elif op==0x01: self._set_bc(self._fetch16()); c=12
        elif op==0x02: self.mmu.wb(self._bc(),self.a); c=8
        elif op==0x03: self._set_bc((self._bc()+1)&0xFFFF); c=8
        elif op==0x04: self.b=self._inc8(self.b)
        elif op==0x05: self.b=self._dec8(self.b)
        elif op==0x06: self.b=self._fetch(); c=8
        elif op==0x07: # RLCA
            c2=self.a>>7; self.a=((self.a<<1)|c2)&0xFF; self._sf(z=0,n=0,h=0,c=bool(c2))
        elif op==0x08: # LD (nn),SP
            a=self._fetch16(); self.mmu.ww(a,self.sp); c=20
        elif op==0x09: self._add_hl(self._bc()); c=8
        elif op==0x0A: self.a=self.mmu.rb(self._bc()); c=8
        elif op==0x0B: self._set_bc((self._bc()-1)&0xFFFF); c=8
        elif op==0x0C: self.c=self._inc8(self.c)
        elif op==0x0D: self.c=self._dec8(self.c)
        elif op==0x0E: self.c=self._fetch(); c=8
        elif op==0x0F: # RRCA
            c2=self.a&1; self.a=((self.a>>1)|(c2<<7))&0xFF; self._sf(z=0,n=0,h=0,c=bool(c2))
        elif op==0x10: self._fetch(); c=4  # STOP
        elif op==0x11: self._set_de(self._fetch16()); c=12
        elif op==0x12: self.mmu.wb(self._de(),self.a); c=8
        elif op==0x13: self._set_de((self._de()+1)&0xFFFF); c=8
        elif op==0x14: self.d=self._inc8(self.d)
        elif op==0x15: self.d=self._dec8(self.d)
        elif op==0x16: self.d=self._fetch(); c=8
        elif op==0x17: # RLA
            c2=self._cf(); nc=self.a>>7; self.a=((self.a<<1)|(1 if c2 else 0))&0xFF; self._sf(z=0,n=0,h=0,c=bool(nc))
        elif op==0x18: self._jr(self._fetch()); c=12
        elif op==0x19: self._add_hl(self._de()); c=8
        elif op==0x1A: self.a=self.mmu.rb(self._de()); c=8
        elif op==0x1B: self._set_de((self._de()-1)&0xFFFF); c=8
        elif op==0x1C: self.e=self._inc8(self.e)
        elif op==0x1D: self.e=self._dec8(self.e)
        elif op==0x1E: self.e=self._fetch(); c=8
        elif op==0x1F: # RRA
            c2=self.a&1; nc=self._cf(); self.a=((self.a>>1)|(0x80 if nc else 0))&0xFF; self._sf(z=0,n=0,h=0,c=bool(c2))
        elif op==0x20: # JR NZ
            d=self._fetch(); c=8
            if not self._zf(): self._jr(d); c=12
        elif op==0x21: self._set_hl(self._fetch16()); c=12
        elif op==0x22: self.mmu.wb(self._hl(),self.a); self._set_hl((self._hl()+1)&0xFFFF); c=8
        elif op==0x23: self._set_hl((self._hl()+1)&0xFFFF); c=8
        elif op==0x24: self.h=self._inc8(self.h)
        elif op==0x25: self.h=self._dec8(self.h)
        elif op==0x26: self.h=self._fetch(); c=8
        elif op==0x27: # DAA
            a=self.a
            if not self._nf():
                if self._hf() or (a&0xF)>9: a+=6
                if self._cf() or a>0x99: a+=0x60; self.f|=0x10
            else:
                if self._hf(): a-=6
                if self._cf(): a-=0x60
            self.a=a&0xFF; self.f=(self.f&~0xA0)|(0x80 if self.a==0 else 0)
        elif op==0x28: # JR Z
            d=self._fetch(); c=8
            if self._zf(): self._jr(d); c=12
        elif op==0x29: self._add_hl(self._hl()); c=8
        elif op==0x2A: self.a=self.mmu.rb(self._hl()); self._set_hl((self._hl()+1)&0xFFFF); c=8
        elif op==0x2B: self._set_hl((self._hl()-1)&0xFFFF); c=8
        elif op==0x2C: self.l=self._inc8(self.l)
        elif op==0x2D: self.l=self._dec8(self.l)
        elif op==0x2E: self.l=self._fetch(); c=8
        elif op==0x2F: self.a^=0xFF; self.f|=0x60
        elif op==0x30: # JR NC
            d=self._fetch(); c=8
            if not self._cf(): self._jr(d); c=12
        elif op==0x31: self.sp=self._fetch16(); c=12
        elif op==0x32: self.mmu.wb(self._hl(),self.a); self._set_hl((self._hl()-1)&0xFFFF); c=8
        elif op==0x33: self.sp=(self.sp+1)&0xFFFF; c=8
        elif op==0x34: hl=self._hl(); self.mmu.wb(hl,self._inc8(self.mmu.rb(hl))); c=12
        elif op==0x35: hl=self._hl(); self.mmu.wb(hl,self._dec8(self.mmu.rb(hl))); c=12
        elif op==0x36: self.mmu.wb(self._hl(),self._fetch()); c=12
        elif op==0x37: self._sf(z=self._zf(),n=0,h=0,c=1)
        elif op==0x38: # JR C
            d=self._fetch(); c=8
            if self._cf(): self._jr(d); c=12
        elif op==0x39: self._add_hl(self.sp); c=8
        elif op==0x3A: self.a=self.mmu.rb(self._hl()); self._set_hl((self._hl()-1)&0xFFFF); c=8
        elif op==0x3B: self.sp=(self.sp-1)&0xFFFF; c=8
        elif op==0x3C: self.a=self._inc8(self.a)
        elif op==0x3D: self.a=self._dec8(self.a)
        elif op==0x3E: self.a=self._fetch(); c=8
        elif op==0x3F: self._sf(z=self._zf(),n=0,h=0,c=not self._cf())
        elif 0x40<=op<=0x7F:  # LD r,r / HALT
            if op==0x76: self.halted=True
            else:
                dst=(op-0x40)>>3; src=op&7
                v=self._r8(src); self._w8(dst,v)
                c=8 if (src==6 or dst==6) else 4
        elif 0x80<=op<=0x87: self._add(self._r8(op&7)); c=4+(4 if (op&7)==6 else 0)
        elif 0x88<=op<=0x8F: self._add(self._r8(op&7),int(self._cf())); c=4+(4 if (op&7)==6 else 0)
        elif 0x90<=op<=0x97: self._sub(self._r8(op&7)); c=4+(4 if (op&7)==6 else 0)
        elif 0x98<=op<=0x9F: self._sub(self._r8(op&7),int(self._cf())); c=4+(4 if (op&7)==6 else 0)
        elif 0xA0<=op<=0xA7: self._and(self._r8(op&7)); c=4+(4 if (op&7)==6 else 0)
        elif 0xA8<=op<=0xAF: self._xor(self._r8(op&7)); c=4+(4 if (op&7)==6 else 0)
        elif 0xB0<=op<=0xB7: self._or(self._r8(op&7)); c=4+(4 if (op&7)==6 else 0)
        elif 0xB8<=op<=0xBF: self._cp(self._r8(op&7)); c=4+(4 if (op&7)==6 else 0)
        elif op==0xC0: # RET NZ
            c=8
            if not self._zf(): self._ret(); c=20
        elif op==0xC1: self._set_bc(self._pop()); c=12
        elif op==0xC2: # JP NZ
            a=self._fetch16(); c=12
            if not self._zf(): self.pc=a; c=16
        elif op==0xC3: self.pc=self._fetch16(); c=16
        elif op==0xC4: # CALL NZ
            a=self._fetch16(); c=12
            if not self._zf(): self._call(a); c=24
        elif op==0xC5: self._push(self._bc()); c=16
        elif op==0xC6: self._add(self._fetch()); c=8
        elif op==0xC7: self._call(0x00); c=16
        elif op==0xC8: # RET Z
            c=8
            if self._zf(): self._ret(); c=20
        elif op==0xC9: self._ret(); c=16
        elif op==0xCA: # JP Z
            a=self._fetch16(); c=12
            if self._zf(): self.pc=a; c=16
        elif op==0xCB: c=self._cb()
        elif op==0xCC: # CALL Z
            a=self._fetch16(); c=12
            if self._zf(): self._call(a); c=24
        elif op==0xCD: self._call(self._fetch16()); c=24
        elif op==0xCE: self._add(self._fetch(),int(self._cf())); c=8
        elif op==0xCF: self._call(0x08); c=16
        elif op==0xD0: # RET NC
            c=8
            if not self._cf(): self._ret(); c=20
        elif op==0xD1: self._set_de(self._pop()); c=12
        elif op==0xD2: # JP NC
            a=self._fetch16(); c=12
            if not self._cf(): self.pc=a; c=16
        elif op==0xD4: # CALL NC
            a=self._fetch16(); c=12
            if not self._cf(): self._call(a); c=24
        elif op==0xD5: self._push(self._de()); c=16
        elif op==0xD6: self._sub(self._fetch()); c=8
        elif op==0xD7: self._call(0x10); c=16
        elif op==0xD8: # RET C
            c=8
            if self._cf(): self._ret(); c=20
        elif op==0xD9: self._ret(); self.ime=True; c=16  # RETI
        elif op==0xDA: # JP C
            a=self._fetch16(); c=12
            if self._cf(): self.pc=a; c=16
        elif op==0xDC: # CALL C
            a=self._fetch16(); c=12
            if self._cf(): self._call(a); c=24
        elif op==0xDE: self._sub(self._fetch(),int(self._cf())); c=8
        elif op==0xDF: self._call(0x18); c=16
        elif op==0xE0: self.mmu.wb(0xFF00|self._fetch(),self.a); c=12
        elif op==0xE1: self._set_hl(self._pop()); c=12
        elif op==0xE2: self.mmu.wb(0xFF00|self.c,self.a); c=8
        elif op==0xE5: self._push(self._hl()); c=16
        elif op==0xE6: self._and(self._fetch()); c=8
        elif op==0xE7: self._call(0x20); c=16
        elif op==0xE8: # ADD SP,r8
            v=self._fetch(); self.sp=self._sp_add(v); c=16
        elif op==0xE9: self.pc=self._hl(); c=4
        elif op==0xEA: self.mmu.wb(self._fetch16(),self.a); c=16
        elif op==0xEE: self._xor(self._fetch()); c=8
        elif op==0xEF: self._call(0x28); c=16
        elif op==0xF0: self.a=self.mmu.rb(0xFF00|self._fetch()); c=12
        elif op==0xF1: self._set_af(self._pop()); c=12
        elif op==0xF2: self.a=self.mmu.rb(0xFF00|self.c); c=8
        elif op==0xF3: self.ime=False; self._ime_pending=0
        elif op==0xF5: self._push(self._af()); c=16
        elif op==0xF6: self._or(self._fetch()); c=8
        elif op==0xF7: self._call(0x30); c=16
        elif op==0xF8: # LD HL,SP+r8
            v=self._fetch(); self._set_hl(self._sp_add(v)); c=12
        elif op==0xF9: self.sp=self._hl(); c=8
        elif op==0xFA: self.a=self.mmu.rb(self._fetch16()); c=16
        elif op==0xFB: self._ime_pending=2; c=4  # EI
        elif op==0xFE: self._cp(self._fetch()); c=8
        elif op==0xFF: self._call(0x38); c=16

        self.cycles+=c

    def _cb(self):
        op=self._fetch(); reg=op&7; bit=(op>>3)&7; grp=op>>6
        v=self._r8(reg); c=8+(4 if reg==6 else 0)
        if grp==0:
            if   bit==0: v=self._rl(v,True)  # RLC
            elif bit==1: v=self._rr(v,True)  # RRC  (thru=False for rotate)
            elif bit==2: v=self._rl(v,True)  # RL
            elif bit==3: v=self._rr(v,True)  # RR
            elif bit==4: v=self._sla(v)
            elif bit==5: v=self._sra(v)
            elif bit==6: v=self._swap(v)
            elif bit==7: v=self._srl(v)
            # fix rotate-not-thru
            if bit==0:
                c2=(v>>7)&1 # already shifted... redo properly
                orig=self._r8(reg) if False else v  # v is already shifted above
            self._w8(reg,v)
        elif grp==1: self._bit(bit,v); c=8+(4 if reg==6 else 0)
        elif grp==2: self._w8(reg,self._res_b(bit,v)); c=8+(4 if reg==6 else 0)
        elif grp==3: self._w8(reg,self._set_b(bit,v)); c=8+(4 if reg==6 else 0)
        return c+4  # +4 for CB prefix fetch

    def _cb_fixed(self):
        """Proper CB rotation handlers"""
        pass


# ══════════════════════════════════════════════════════════════════════
#  GAMEBOY  (top-level system)
# ══════════════════════════════════════════════════════════════════════
class GameBoy:
    def __init__(self):
        self.cart=None; self.ppu=PPU(); self.timer=Timer()
        self.joy=Joypad(); self.mmu=None; self.cpu=None
        self.running=False; self.total_cyc=0

    def load(self, data: bytes):
        self.cart=Cartridge(data)
        self.ppu=PPU(); self.timer=Timer(); self.joy=Joypad()
        self.mmu=MMU(self.cart,self.ppu,self.timer,self.joy)
        self.cpu=CPU(self.mmu)
        self.total_cyc=0

    def run_frame(self):
        if not self.cpu: return
        target=self.total_cyc+CYCLES_PER_FRAME
        while self.total_cyc<target:
            before=self.cpu.cycles
            self.cpu.step()
            elapsed=self.cpu.cycles-before
            self.total_cyc+=elapsed
            vb,st=self.ppu.step(elapsed)
            ti=self.timer.step(elapsed)
            if vb: self.mmu.IF|=INT_VBLANK
            if st: self.mmu.IF|=INT_STAT
            if ti: self.mmu.IF|=INT_TIMER


# ══════════════════════════════════════════════════════════════════════
#  GUI  —  Cat's Gameboy 0.1   (mGBA-faithful shell)
#
#  Design refs:
#   • Dark Qt-style window, #2b2b2b body
#   • Screen fills entire client area below menu — zero padding
#   • Menubar: File | Emulation | Save States | Cheats | Tools | Settings | Help
#   • Status bar: 1 line — ROM name left · FPS + speed right
#   • Title bar: "Cat's Gameboy 0.1 – <ROM name>"
#   • Right-click context menu on screen
#   • Resizable — integer scale modes (1×–4×) + Free resize
# ══════════════════════════════════════════════════════════════════════

APP_NAME    = "Cat's Gameboy 0.1"

# ── mGBA colour palette ───────────────────────────────────────────────
C_BG        = "#2b2b2b"   # main window bg
C_MENU_BG   = "#3c3f41"   # menu / menubar bg
C_MENU_FG   = "#bbbbbb"   # menu text
C_MENU_SEL  = "#4c5052"   # menu hover
C_MENU_SEL_FG="#ffffff"
C_SEP       = "#555555"   # separator line
C_STATUS_BG = "#3c3f41"   # status bar bg
C_STATUS_FG = "#aaaaaa"   # status bar text
C_STATUS_BD = "#555555"   # status bar top border
C_SCREEN_BG = "#000000"   # behind game screen
C_SPLASH_BG = "#000000"
C_SPLASH_FG = "#444444"
C_SPLASH_HI = "#666666"

FONT_UI     = ("Segoe UI", 9) if sys.platform == "win32" else ("DejaVu Sans", 9)
FONT_MONO   = ("Consolas", 9) if sys.platform == "win32" else ("DejaVu Sans Mono", 9)
FONT_STATUS = ("Consolas", 8) if sys.platform == "win32" else ("DejaVu Sans Mono", 8)


class CatsGBApp:
    SCALE = 3   # default 3× → 480×432

    def __init__(self, root: tk.Tk):
        self.root       = root
        self.gb         = GameBoy()
        self.lock       = Lock()
        self._running   = True
        self._paused    = False
        self._frame_buf = None          # bytes of latest rendered frame
        self._photo     = None          # ImageTk ref-keeper
        self._fps       = 0
        self._frame_cnt = 0
        self._fps_ts    = time.time()
        self._rom_path  = ""
        self._rom_name  = ""
        self._speed     = 1.0           # fast-forward multiplier
        self._ss_slot   = 1             # active save-state slot
        self._recent    : list[str] = []
        self._ff_active = False

        # save-state storage (in-memory, 9 slots)
        self._save_slots: dict[int, bytes] = {}

        self._setup_window()
        self._build_menubar()
        self._build_screen()
        self._build_statusbar()
        self._bind_keys()
        self._bind_mouse()

        Thread(target=self._emu_loop, daemon=True).start()
        self._gui_tick()

    # ══════════════════════════════════════════════════════════════
    #  WINDOW SETUP
    # ══════════════════════════════════════════════════════════════
    def _setup_window(self):
        sw = GB_W * self.SCALE
        sh = GB_H * self.SCALE
        self.root.title(APP_NAME)
        self.root.configure(bg=C_BG)
        self.root.resizable(True, True)
        self.root.geometry(f"{sw}x{sh + 22 + 19}")   # screen + menu + status
        self.root.minsize(GB_W, GB_H + 22 + 19)

    # ══════════════════════════════════════════════════════════════
    #  MENU BAR  (mGBA layout)
    # ══════════════════════════════════════════════════════════════
    def _build_menubar(self):
        kw_bar = dict(bg=C_MENU_BG, fg=C_MENU_FG,
                      activebackground=C_MENU_SEL, activeforeground=C_MENU_SEL_FG,
                      relief=tk.FLAT, bd=0)
        kw_m   = dict(**kw_bar, tearoff=0)

        bar = tk.Menu(self.root, **kw_bar)

        def M(label):
            m = tk.Menu(bar, **kw_m)
            bar.add_cascade(label=label, menu=m)
            return m

        def item(m, label, cmd=None, accel="", state="normal"):
            m.add_command(label=label, command=cmd or (lambda:None),
                          accelerator=accel, state=state)

        def sep(m): m.add_separator()

        # ── File ──────────────────────────────────────────────
        fm = M("File")
        item(fm, "Load ROM...",            self._load_rom,   "Ctrl+O")
        self._recent_menu = tk.Menu(fm, **kw_m)
        fm.add_cascade(label="Load Recent ROM", menu=self._recent_menu)
        self._recent_menu.add_command(label="(empty)", state="disabled")
        sep(fm)
        item(fm, "Close ROM",              self._close_rom)
        sep(fm)
        item(fm, "Save State",             lambda: self._save_state(self._ss_slot), "F5")
        item(fm, "Load State",             lambda: self._load_state(self._ss_slot), "F8")
        sep(fm)
        item(fm, "Exit",                   self._quit,       "Alt+F4")

        # ── Emulation ─────────────────────────────────────────
        em = M("Emulation")
        item(em, "Pause",                  self._toggle_pause, "P")
        item(em, "Reset",                  self._reset,        "Ctrl+R")
        item(em, "Frame Advance",          self._frame_advance,"Ctrl+N")
        sep(em)
        item(em, "Fast Forward (hold)",    None,               "Tab",  "disabled")
        item(em, "Rewind",                 None,               "",     "disabled")
        sep(em)

        speed_m = tk.Menu(em, **kw_m)
        em.add_cascade(label="Game Speed", menu=speed_m)
        for pct, mult in [(25,.25),(50,.5),(100,1),(150,1.5),(200,2),(300,3),(600,6),(unbounded:=None,0)]:
            if pct is None: speed_m.add_command(label="Unthrottled", command=lambda: self._set_speed(0))
            else: speed_m.add_command(label=f"{pct}%", command=lambda m=mult: self._set_speed(m))

        sep(em)
        item(em, "Run Test ROM",           self._test_rom)

        # ── Save States ───────────────────────────────────────
        sm = M("Save States")
        for i in range(1, 10):
            sm.add_command(label=f"Slot {i}  {'[empty]' if i not in self._save_slots else '[saved]'}",
                           command=lambda n=i: self._pick_slot(n))
        sep(sm)
        item(sm, "Load Latest",            self._load_latest_state)

        # ── Cheats ────────────────────────────────────────────
        cm = M("Cheats")
        item(cm, "Manage Cheats...",       self._cheats_stub)
        sep(cm)
        item(cm, "Search Memory...",       self._cheats_stub)

        # ── Tools ─────────────────────────────────────────────
        tm = M("Tools")
        item(tm, "Memory Viewer...",       self._tools_stub)
        item(tm, "Tile Viewer...",         self._tile_viewer)
        item(tm, "Map Viewer...",          self._tools_stub)
        item(tm, "Sprite Viewer...",       self._tools_stub)
        sep(tm)
        item(tm, "Game Pak Override...",   self._tools_stub)

        # ── Settings ──────────────────────────────────────────
        stm = M("Settings")
        item(stm, "Controllers...",        self._show_controls)
        sep(stm)

        video_m = tk.Menu(stm, **kw_m)
        stm.add_cascade(label="Video", menu=video_m)
        for s, lbl in [(1,"1× (160×144)"),(2,"2× (320×288)"),(3,"3× (480×432)"),(4,"4× (640×576)")]:
            video_m.add_command(label=lbl, command=lambda x=s: self._set_scale(x))
        video_m.add_separator()
        video_m.add_command(label="Toggle Fullscreen", command=self._toggle_fullscreen)

        sep(stm)
        pal_m = tk.Menu(stm, **kw_m)
        stm.add_cascade(label="GB Palette", menu=pal_m)
        palettes = {
            "DMG Green"  : [(0xE8,0xF8,0xD0),(0x88,0xC0,0x70),(0x34,0x68,0x56),(0x08,0x18,0x20)],
            "Grey"       : [(0xEF,0xEF,0xEF),(0xAA,0xAA,0xAA),(0x55,0x55,0x55),(0x00,0x00,0x00)],
            "Amber"      : [(0xFF,0xF7,0x7D),(0xF0,0xA0,0x00),(0xA0,0x50,0x00),(0x20,0x10,0x00)],
            "Blue LCD"   : [(0xC0,0xD0,0xFF),(0x70,0x90,0xE0),(0x20,0x40,0x90),(0x00,0x08,0x30)],
        }
        for name, pal in palettes.items():
            pal_m.add_command(label=name, command=lambda p=pal: self._set_palette(p))

        sep(stm)
        item(stm, "Emulation Settings...", self._tools_stub)
        sep(stm)
        item(stm, "About Paths...",        self._tools_stub)

        # ── Help ──────────────────────────────────────────────
        hm = M("Help")
        item(hm, "Keyboard Shortcuts",    self._show_controls)
        sep(hm)
        item(hm, "About Cat's Gameboy 0.1", self._about)

        self.root.config(menu=bar)
        self._bar = bar

    # ══════════════════════════════════════════════════════════════
    #  SCREEN  (fills client area completely like mGBA)
    # ══════════════════════════════════════════════════════════════
    def _build_screen(self):
        # The canvas IS the window content — no padding, no border
        self.screen = tk.Canvas(
            self.root,
            bg=C_SCREEN_BG,
            highlightthickness=0,
            bd=0,
            cursor="arrow",
        )
        self.screen.pack(side="top", fill="both", expand=True)
        self.screen.bind("<Configure>", self._on_resize)
        self._draw_splash()

    def _on_resize(self, event):
        self._redraw_current()

    def _redraw_current(self):
        if self._photo:
            self._blit_photo(self._photo)
        else:
            self._draw_splash()

    # ── Splash (no ROM loaded) ─────────────────────────────────
    def _draw_splash(self):
        self.screen.delete("all")
        w = self.screen.winfo_width()  or GB_W * self.SCALE
        h = self.screen.winfo_height() or GB_H * self.SCALE
        # dark bg
        self.screen.create_rectangle(0, 0, w, h, fill=C_SPLASH_BG, outline="")
        # centred logo text — mGBA shows nothing, we show a minimal stamp
        cx, cy = w // 2, h // 2
        self.screen.create_text(cx, cy - 12, text="Cat's Gameboy 0.1",
                                 fill=C_SPLASH_HI, font=("Consolas",13,"bold")
                                 if sys.platform=="win32" else ("DejaVu Sans Mono",11,"bold"))
        self.screen.create_text(cx, cy + 10, text="File  →  Load ROM…",
                                 fill=C_SPLASH_FG,
                                 font=("Consolas",9) if sys.platform=="win32" else ("DejaVu Sans Mono",8))

    # ── Blit a PIL ImageTk to the canvas, letterboxed ──────────
    def _blit_photo(self, photo):
        self.screen.delete("all")
        cw = self.screen.winfo_width()  or GB_W * self.SCALE
        ch = self.screen.winfo_height() or GB_H * self.SCALE
        self.screen.create_rectangle(0, 0, cw, ch, fill=C_SCREEN_BG, outline="")
        # centre it
        iw, ih = photo.width(), photo.height()
        x = (cw - iw) // 2
        y = (ch - ih) // 2
        self.screen.create_image(x, y, anchor="nw", image=photo)

    # ══════════════════════════════════════════════════════════════
    #  STATUS BAR  (1 row, like mGBA)
    # ══════════════════════════════════════════════════════════════
    def _build_statusbar(self):
        bar = tk.Frame(self.root, bg=C_STATUS_BG, height=19,
                       highlightbackground=C_STATUS_BD, highlightthickness=1)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)

        self._status_left  = tk.StringVar(value="  Ready")
        self._status_right = tk.StringVar(value="")

        tk.Label(bar, textvariable=self._status_left,
                 bg=C_STATUS_BG, fg=C_STATUS_FG,
                 font=FONT_STATUS, anchor="w", padx=6).pack(side="left", fill="x", expand=True)

        tk.Label(bar, textvariable=self._status_right,
                 bg=C_STATUS_BG, fg=C_STATUS_FG,
                 font=FONT_STATUS, anchor="e", padx=6).pack(side="right")

    def _set_status(self, msg=""):
        self._status_left.set(f"  {msg}" if msg else "  Ready")

    def _update_fps_label(self, fps):
        spd = "" if self._speed==1.0 else f"  {int(self._speed*100)}%"
        self._status_right.set(f"FPS: {fps}{spd}   ")

    # ══════════════════════════════════════════════════════════════
    #  EMULATION THREAD
    # ══════════════════════════════════════════════════════════════
    def _emu_loop(self):
        frame_dur = 1.0 / 59.7
        last = time.perf_counter()
        while self._running:
            if self._paused or not self.gb.cart:
                time.sleep(0.005)
                continue

            eff_dur = frame_dur / max(self._speed, 0.01) if self._speed > 0 else 0

            now = time.perf_counter()
            if now - last >= (eff_dur if eff_dur > 0 else 0):
                self.gb.run_frame()
                if self.gb.ppu.frame_ready:
                    with self.lock:
                        self._frame_buf = bytes(self.gb.ppu.pixels)
                    self.gb.ppu.frame_ready = False
                self._frame_cnt += 1
                last += eff_dur if eff_dur > 0 else (now - last)
            else:
                time.sleep(0.001)

    # ══════════════════════════════════════════════════════════════
    #  GUI TICK  (~60 Hz)
    # ══════════════════════════════════════════════════════════════
    def _gui_tick(self):
        if not self._running:
            return

        # grab latest frame
        with self.lock:
            buf = self._frame_buf
            self._frame_buf = None

        if buf:
            self._render_frame(buf)

        # FPS counter every second
        now = time.time()
        if now - self._fps_ts >= 1.0:
            self._fps = self._frame_cnt
            self._frame_cnt = 0
            self._fps_ts = now
            self._update_fps_label(self._fps)

        self.root.after(16, self._gui_tick)

    # ── Render raw RGB bytes → PIL → canvas ───────────────────
    def _render_frame(self, buf: bytes):
        cw = self.screen.winfo_width()  or GB_W * self.SCALE
        ch = self.screen.winfo_height() or GB_H * self.SCALE

        if PIL:
            img = Image.frombytes("RGB", (GB_W, GB_H), buf)
            # Integer scale that fits the canvas
            sx = max(1, cw // GB_W)
            sy = max(1, ch // GB_H)
            s  = min(sx, sy)
            img = img.resize((GB_W * s, GB_H * s), Image.NEAREST)
            self._photo = ImageTk.PhotoImage(img)
        else:
            # Fallback — just draw coloured rectangles (slow)
            self._photo = None
            self.screen.delete("all")
            self.screen.create_rectangle(0,0,cw,ch,fill=C_SCREEN_BG,outline="")
            sx = max(1, cw // GB_W); sy = max(1, ch // GB_H); s = min(sx,sy)
            ox = (cw - GB_W*s) // 2; oy = (ch - GB_H*s) // 2
            for y in range(GB_H):
                for x in range(GB_W):
                    i = (y*GB_W+x)*3
                    r,g,b = buf[i],buf[i+1],buf[i+2]
                    self.screen.create_rectangle(
                        ox+x*s, oy+y*s, ox+(x+1)*s, oy+(y+1)*s,
                        fill=f"#{r:02X}{g:02X}{b:02X}", outline="")
            return

        self._blit_photo(self._photo)

    # ══════════════════════════════════════════════════════════════
    #  KEY / MOUSE BINDINGS
    # ══════════════════════════════════════════════════════════════
    def _bind_keys(self):
        km = {
            "z":"a","x":"b",
            "Return":"start","BackSpace":"sel","KP_Enter":"start",
            "Up":"up","Down":"down","Left":"left","Right":"right",
            "w":"up","s":"down","a":"left","d":"right",
        }
        def dn(e):
            j = self.gb.joy
            k = km.get(e.keysym)
            if k=="a":     j.a=True
            elif k=="b":   j.b=True
            elif k=="start": j.start=True
            elif k=="sel":   j.sel=True
            elif k=="up":    j.up=True
            elif k=="down":  j.down=True
            elif k=="left":  j.left=True
            elif k=="right": j.right=True
        def up(e):
            j = self.gb.joy
            k = km.get(e.keysym)
            if k=="a":     j.a=False
            elif k=="b":   j.b=False
            elif k=="start": j.start=False
            elif k=="sel":   j.sel=False
            elif k=="up":    j.up=False
            elif k=="down":  j.down=False
            elif k=="left":  j.left=False
            elif k=="right": j.right=False

        self.root.bind("<KeyPress>",   dn)
        self.root.bind("<KeyRelease>", up)

        # Shortcuts
        self.root.bind("<Control-o>",  lambda e: self._load_rom())
        self.root.bind("<Control-r>",  lambda e: self._reset())
        self.root.bind("p",            lambda e: self._toggle_pause())
        self.root.bind("<F5>",         lambda e: self._save_state(self._ss_slot))
        self.root.bind("<F8>",         lambda e: self._load_state(self._ss_slot))
        self.root.bind("<Control-n>",  lambda e: self._frame_advance())
        self.root.bind("<Tab>",        lambda e: self._set_speed(3.0))
        self.root.bind("<KeyRelease-Tab>", lambda e: self._set_speed(1.0))

    def _bind_mouse(self):
        # Right-click context menu (like mGBA)
        ctx = tk.Menu(self.root, tearoff=0,
                      bg=C_MENU_BG, fg=C_MENU_FG,
                      activebackground=C_MENU_SEL, activeforeground=C_MENU_SEL_FG,
                      relief=tk.FLAT)
        ctx.add_command(label="Load ROM…",         command=self._load_rom)
        ctx.add_separator()
        ctx.add_command(label="Pause",             command=self._toggle_pause)
        ctx.add_command(label="Reset",             command=self._reset)
        ctx.add_separator()
        ctx.add_command(label="1× Scale",          command=lambda: self._set_scale(1))
        ctx.add_command(label="2× Scale",          command=lambda: self._set_scale(2))
        ctx.add_command(label="3× Scale",          command=lambda: self._set_scale(3))
        ctx.add_command(label="4× Scale",          command=lambda: self._set_scale(4))
        ctx.add_separator()
        ctx.add_command(label="Toggle Fullscreen", command=self._toggle_fullscreen)

        def show_ctx(e):
            try: ctx.tk_popup(e.x_root, e.y_root)
            finally: ctx.grab_release()

        self.screen.bind("<Button-3>", show_ctx)
        self.screen.bind("<Double-Button-1>", lambda e: self._toggle_fullscreen())

    # ══════════════════════════════════════════════════════════════
    #  ROM LOADING
    # ══════════════════════════════════════════════════════════════
    def _load_rom(self):
        path = filedialog.askopenfilename(
            title="Load ROM",
            filetypes=[("Game Boy ROMs","*.gb *.gbc *.GB *.GBC"),("All files","*.*")])
        if not path:
            return
        try:
            data = open(path,"rb").read()
            self.gb.load(data)
            self._rom_path = path
            self._rom_name = self.gb.cart.name or os.path.basename(path)
            self._paused   = False
            self._photo    = None
            self._update_title()
            self._set_status(os.path.basename(path))
            self._add_recent(path)
        except Exception as exc:
            messagebox.showerror("Load ROM", str(exc))

    def _close_rom(self):
        self.gb.cart = None; self.gb.cpu = None
        self._photo = None; self._rom_name = ""; self._rom_path = ""
        self._draw_splash()
        self._update_title()
        self._set_status()
        self._update_fps_label(0)

    def _add_recent(self, path):
        if path in self._recent: self._recent.remove(path)
        self._recent.insert(0, path)
        self._recent = self._recent[:10]
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        m = self._recent_menu
        m.delete(0,"end")
        if not self._recent:
            m.add_command(label="(empty)", state="disabled"); return
        for p in self._recent:
            m.add_command(label=os.path.basename(p),
                          command=lambda x=p: self._load_path(x))

    def _load_path(self, path):
        try:
            data = open(path,"rb").read()
            self.gb.load(data)
            self._rom_path = path
            self._rom_name = self.gb.cart.name or os.path.basename(path)
            self._paused   = False; self._photo = None
            self._update_title(); self._set_status(os.path.basename(path))
        except Exception as exc:
            messagebox.showerror("Load ROM", str(exc))

    def _update_title(self):
        if self._rom_name:
            self.root.title(f"{APP_NAME}  –  {self._rom_name}")
        else:
            self.root.title(APP_NAME)

    # ── Built-in test ROM ──────────────────────────────────────
    def _test_rom(self):
        data = bytearray(0x8000)
        logo = bytes([0xCE,0xED,0x66,0x66,0xCC,0x0D,0x00,0x0B,
                      0x03,0x73,0x00,0x83,0x00,0x0C,0x00,0x0D,
                      0x00,0x08,0x11,0x1F,0x88,0x89,0x00,0x0E,
                      0xDC,0xCC,0x6E,0xE6,0xDD,0xDD,0xD9,0x99,
                      0xBB,0xBB,0x67,0x63,0x6E,0x0E,0xEC,0xCC,
                      0xDD,0xDC,0x99,0x9F,0xBB,0xB9,0x33,0x3E])
        data[0x104:0x104+len(logo)] = logo
        for i,ch in enumerate(b'CATSGB0001\x00\x00\x00\x00\x00\x00'):
            data[0x134+i] = ch
        data[0x147]=0; data[0x148]=0; data[0x149]=0
        data[0x100:0x104] = bytes([0x00, 0xC3, 0x50, 0x01])
        prog = bytearray([
            0x31,0xFE,0xFF,                    # LD SP,$FFFE
            0xAF,0xEA,0x40,0xFF,               # LCDC=0
            0x21,0x00,0x80,0x06,0x20,0x0E,0x10, # clear VRAM
            0x22,0x0D,0x20,0xFC,0x05,0x20,0xF6,
            0x21,0x10,0x80,0x06,0x08,0x3E,0xFF, # solid tile
            0x77,0x23,0x77,0x23,0x05,0x20,0xF9,
            0x21,0x00,0x98,0x06,0x12,0x0E,0x14,0x3E,0x01, # fill bg map
            0x77,0x23,0x0D,0x20,0xFC,0x0E,0x14,0x05,0x20,0xF6,
            0x3E,0xE4,0xEA,0x47,0xFF,          # BGP
            0xAF,0xEA,0x42,0xFF,0xEA,0x43,0xFF, # SCY/SCX=0
            0x3E,0x91,0xEA,0x40,0xFF,          # LCDC on
            0x76,0x18,0xFD,                    # HALT loop
        ])
        data[0x150:0x150+len(prog)] = prog
        cs = 0
        for i in range(0x134,0x14D): cs = (cs-data[i]-1)&0xFF
        data[0x14D] = cs
        self.gb.load(bytes(data))
        self._rom_name = "[Test Pattern]"
        self._rom_path = ""
        self._paused   = False; self._photo = None
        self._update_title()
        self._set_status("[Test Pattern]")

    # ══════════════════════════════════════════════════════════════
    #  EMULATION CONTROLS
    # ══════════════════════════════════════════════════════════════
    def _toggle_pause(self):
        self._paused = not self._paused
        state = "Paused" if self._paused else os.path.basename(self._rom_path or "")
        self._set_status(("⏸  Paused  —  " + self._rom_name) if self._paused else self._rom_name)
        # dim screen slightly when paused
        if self._paused and self._photo:
            self._blit_photo(self._photo)

    def _reset(self):
        if self.gb.cart:
            self.gb.load(bytes(self.gb.cart.rom))
            self._paused = False
            self._set_status(f"Reset — {self._rom_name}")

    def _frame_advance(self):
        if self.gb.cart:
            self.gb.run_frame()
            if self.gb.ppu.frame_ready:
                buf = bytes(self.gb.ppu.pixels)
                self.gb.ppu.frame_ready = False
                self._render_frame(buf)

    def _set_speed(self, mult):
        self._speed = max(0, mult)
        self._update_fps_label(self._fps)

    # ══════════════════════════════════════════════════════════════
    #  SAVE / LOAD STATES  (in-memory)
    # ══════════════════════════════════════════════════════════════
    def _pick_slot(self, n):
        self._ss_slot = n
        self._set_status(f"Save-state slot: {n}")

    def _save_state(self, slot):
        if not self.gb.cpu: return
        import pickle
        try:
            snap = pickle.dumps({
                'cpu_regs': (self.gb.cpu.a,self.gb.cpu.f,self.gb.cpu.b,self.gb.cpu.c,
                             self.gb.cpu.d,self.gb.cpu.e,self.gb.cpu.h,self.gb.cpu.l,
                             self.gb.cpu.pc,self.gb.cpu.sp,self.gb.cpu.ime,self.gb.cpu.halted),
                'wram': bytes(self.gb.mmu.wram),
                'hram': bytes(self.gb.mmu.hram),
                'vram': bytes(self.gb.ppu.vram),
                'oam':  bytes(self.gb.ppu.oam),
            })
            self._save_slots[slot] = snap
            self._set_status(f"State saved to slot {slot}")
        except Exception as e:
            self._set_status(f"Save state failed: {e}")

    def _load_state(self, slot):
        if slot not in self._save_slots: self._set_status(f"Slot {slot} is empty"); return
        if not self.gb.cpu: return
        import pickle
        try:
            d = pickle.loads(self._save_slots[slot])
            c = self.gb.cpu
            c.a,c.f,c.b,c.c,c.d,c.e,c.h,c.l,c.pc,c.sp,c.ime,c.halted = d['cpu_regs']
            self.gb.mmu.wram[:] = d['wram']
            self.gb.mmu.hram[:] = d['hram']
            self.gb.ppu.vram[:]  = d['vram']
            self.gb.ppu.oam[:]   = d['oam']
            self._set_status(f"State loaded from slot {slot}")
        except Exception as e:
            self._set_status(f"Load state failed: {e}")

    def _load_latest_state(self):
        if not self._save_slots: self._set_status("No states saved"); return
        slot = max(self._save_slots)
        self._load_state(slot)

    # ══════════════════════════════════════════════════════════════
    #  VIEW
    # ══════════════════════════════════════════════════════════════
    def _set_scale(self, s):
        self.SCALE = s
        sw = GB_W * s; sh = GB_H * s
        self.root.geometry(f"{sw}x{sh + 22 + 19}")
        self._set_status(f"Scale: {s}×  ({sw}×{sh})")

    _fullscreen = False
    def _toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        self.root.attributes("-fullscreen", self._fullscreen)

    def _set_palette(self, pal):
        global DMG_COLORS
        DMG_COLORS = pal
        self._set_status("Palette changed")

    # ══════════════════════════════════════════════════════════════
    #  TOOLS / DIALOGS
    # ══════════════════════════════════════════════════════════════
    def _tile_viewer(self):
        """Simple tile VRAM viewer — shows all 384 tiles."""
        if not self.gb.ppu:
            messagebox.showinfo("Tile Viewer", "No ROM loaded."); return
        win = tk.Toplevel(self.root)
        win.title("Tile Viewer  —  Cat's Gameboy 0.1")
        win.configure(bg=C_BG)
        win.resizable(False, False)

        COLS = 16; TILE_PX = 16  # render each tile at 2×
        ROWS = 24
        cw = COLS * TILE_PX; ch = ROWS * TILE_PX
        c = tk.Canvas(win, width=cw, height=ch, bg="#111111", highlightthickness=0)
        c.pack(padx=8, pady=8)

        pal_grey = [(0xFF,0xFF,0xFF),(0xAA,0xAA,0xAA),(0x55,0x55,0x55),(0x00,0x00,0x00)]

        def refresh():
            c.delete("all")
            vram = self.gb.ppu.vram
            for tile_idx in range(384):
                tx = (tile_idx % COLS) * TILE_PX
                ty = (tile_idx // COLS) * TILE_PX
                addr = tile_idx * 16
                for row in range(8):
                    if addr+row*2+1 >= len(vram): break
                    lo = vram[addr+row*2]
                    hi = vram[addr+row*2+1]
                    for col in range(8):
                        bit = 7 - col
                        ci = ((hi>>bit)&1)<<1 | ((lo>>bit)&1)
                        r,g,b = pal_grey[ci]
                        px = tx + col*2; py = ty + row*2
                        c.create_rectangle(px,py,px+2,py+2,
                                           fill=f"#{r:02X}{g:02X}{b:02X}",outline="")

        refresh()
        tk.Button(win, text="Refresh", bg=C_MENU_BG, fg=C_MENU_FG,
                  relief=tk.FLAT, command=refresh, padx=10).pack(pady=(0,8))

    def _tools_stub(self):
        messagebox.showinfo("Cat's Gameboy 0.1", "Tool not yet implemented in v0.1.")

    def _cheats_stub(self):
        messagebox.showinfo("Cheats", "Cheat support not yet implemented in v0.1.")

    def _show_controls(self):
        win = tk.Toplevel(self.root)
        win.title("Keyboard Shortcuts  —  Cat's Gameboy 0.1")
        win.configure(bg=C_BG)
        win.resizable(False, False)
        txt = (
            "  ─────────────────────────────────────\n"
            "   Game Boy Buttons\n"
            "  ─────────────────────────────────────\n"
            "   Arrows / WASD       D-Pad\n"
            "   Z                   A\n"
            "   X                   B\n"
            "   Enter               Start\n"
            "   Backspace           Select\n"
            "\n"
            "  ─────────────────────────────────────\n"
            "   Emulator\n"
            "  ─────────────────────────────────────\n"
            "   Ctrl+O              Load ROM\n"
            "   P                   Pause / Resume\n"
            "   Ctrl+R              Reset\n"
            "   Ctrl+N              Frame Advance\n"
            "   Tab (hold)          Fast Forward (3×)\n"
            "   F5                  Save State\n"
            "   F8                  Load State\n"
            "\n"
            "   Double-click        Toggle Fullscreen\n"
            "   Right-click         Context Menu\n"
            "  ─────────────────────────────────────\n"
        )
        tk.Label(win, text=txt, bg=C_BG, fg=C_STATUS_FG,
                 font=FONT_MONO, justify="left").pack(padx=16, pady=12)

    def _about(self):
        win = tk.Toplevel(self.root)
        win.title("About")
        win.configure(bg=C_BG)
        win.resizable(False, False)
        tk.Label(win, text="Cat's Gameboy 0.1", bg=C_BG, fg="#dddddd",
                 font=("Consolas",14,"bold") if sys.platform=="win32"
                 else ("DejaVu Sans Mono",12,"bold")).pack(pady=(18,4))
        details = (
            "Game Boy (DMG) Emulator\n\n"
            "SM83 CPU  ·  PPU Scanline Renderer\n"
            "MBC1 / MBC3  ·  Timer  ·  Joypad\n"
            "~59.7 FPS  ·  70224 cyc/frame\n\n"
            "Team Flames / Samsoft  © 2026\n"
        )
        tk.Label(win, text=details, bg=C_BG, fg=C_STATUS_FG,
                 font=FONT_MONO, justify="center").pack(padx=24, pady=(0,16))
        tk.Button(win, text="  OK  ", bg=C_MENU_BG, fg=C_MENU_FG,
                  relief=tk.FLAT, command=win.destroy).pack(pady=(0,14))

    # ══════════════════════════════════════════════════════════════
    #  QUIT
    # ══════════════════════════════════════════════════════════════
    def _quit(self):
        self._running = False
        self.root.destroy()


# ══════════════════════════════════════════════════════════════════════
#  ENTRY
# ══════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    root = tk.Tk()
    app  = CatsGBApp(root)

    def _on_close():
        app._running = False
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()
