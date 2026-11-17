#!/usr/bin/env python3
"""
fake_cxa.py - a pretend CXA61 on a pseudo-terminal, for trying cxa_update.py
without an amplifier (and for testing it).

    python3 fake_cxa.py                 # prints the device path to use
    python3 cxa_update.py probe  --port <that path>
    python3 cxa_update.py update --port <that path>

It answers the way the vendor's updater expects an amplifier to answer.  The
amplifier side of the protocol is not documented; this is a model inferred from
the updater program and from one transcript of a real CXA61 being updated, not
a copy of real amplifier firmware.
"""

import argparse
import os
import pty
import select
import sys
import termios
import time
import tty

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cxa_update import crc16, crc8, checksum8, SLOTS, PAGE_SIZE  # noqa: E402

FLASH_SIZE = 0x400000


class FakeAmp:
    def __init__(self, args):
        self.args = args
        self.flash = bytearray(b"\xFF" * FLASH_SIZE)
        self.cur = {4: tuple(args.mcu), 0: tuple(args.usb)}
        self.on = not args.standby
        self.rx = bytearray()
        self.out = bytearray()
        self.seq = 0
        self.prog_addr = None
        self.frames = 0
        self.pending = []          # (time, bytes) to send later
        self.capture = open(args.capture, "wb") if args.capture else None

    # ---- flash store helpers ----
    def slot_info(self, i):
        name, ident, start, header = SLOTS[i]
        f = self.flash
        if f[header + 1] != ident:
            return None
        size = int.from_bytes(f[header + 5:header + 9], "big")
        if size <= 0 or start + size > FLASH_SIZE:
            return None
        calc = checksum8(f, start + 16, size - 16) if start == header else checksum8(f, start, size)
        if calc != f[header + 2]:
            return None
        return (f[header + 3], f[header + 4]), start, size

    def versions_frame(self, sub, table):
        mask = 0
        d = bytearray(13)
        for i, bit, pos in ((4, 0x01, 1), (3, 0x02, 3), (0, 0x04, 5), (5, 0x20, 11)):
            v = table.get(i)
            if v is None:
                mask |= bit
            else:
                d[pos], d[pos + 1] = v
        d[0] = mask
        return bytes([0x21, 0x0F, sub, len(d)]) + bytes(d)

    def stored_versions(self):
        t = {}
        for i in SLOTS:
            info = self.slot_info(i)
            if info:
                t[i] = info[0]
        return t

    # ---- protocol ----
    def send(self, data, delay=0.0):
        if delay:
            self.pending.append((time.monotonic() + delay, bytes(data)))
        else:
            self.out += data

    def handle_text(self, line):
        if line == b"#13,03":
            self.send(b"#14,03,%s\r" % self.args.model.encode())
        elif line == b"#01,01":
            self.send(b"#02,01,%d\r" % (1 if self.on else 0))
        elif line == b"#01,02,1":
            self.on = True
            self.send(b"#02,01,1\r", 0.3)
            self.send(b"#02,03,0\r", 1.0)
        elif line.startswith(b"#03,04,"):
            self.send(b"#04,01," + line[7:9] + b"\r")
        else:
            self.send(b"#00,02\r")

    def handle_bin(self, grp, cmd, data):
        self.frames += 1
        a = self.args
        if a.die_after and self.frames > a.die_after:
            return
        if grp == 0xAA and cmd == 0x8B:
            self.send(bytes([0x21, 0xAA, 0x0B, 0x03, 0xEF, 0x40, 0x16]))
        elif grp == 0xAA and cmd == 0x8A and len(data) == 4:
            a2, a1, a0, n = data
            if a2 & 0xC0 == 0x80:
                addr = ((a2 & 0x3F) << 16) | (a1 << 8) | a0
                crc = crc16(self.flash, addr, PAGE_SIZE)
            elif a2 == 0x40:
                info = self.slot_info(a0 >> 4)
                if not info:
                    self.send(bytes([0x21, 0xAA, 0x0F, 0x00]))
                    return
                crc = crc16(self.flash, info[1], info[2])
            else:
                self.send(bytes([0x21, 0xAA, 0x0F, 0x00]))
                return
            self.send(bytes([0x21, 0xAA, 0x0A, 0x05, a2, a1, a0, crc >> 8, crc & 0xFF]))
        elif grp == 0xAA and cmd == 0x0A:
            if len(data) == 0:
                self.prog_addr = None
                self.send(bytes([0x21, 0xAA, 0x8A, 0x00]))
            elif data[0] == 0xF5 and len(data) == 7 and self.prog_addr is None:
                if crc8(data[1:6]) != data[6]:
                    self.send(bytes([0x21, 0xAA, 0x8A, 0x01, 0xE1]))
                    return
                addr = int.from_bytes(data[1:5], "big")
                if data[5] == 0x20:
                    self.flash[addr:addr + PAGE_SIZE] = b"\xFF" * PAGE_SIZE
                    self.send(bytes([0x21, 0xAA, 0x8A, 0x00]))
                elif data[5] == 0x02:
                    self.prog_addr = addr
                    self.seq = 1
                    self.send(bytes([0x21, 0xAA, 0x8A, 0x03, self.seq, 0x00, a.max_block]))
                else:
                    self.send(bytes([0x21, 0xAA, 0x8A, 0x01, 0xE2]))
            elif self.prog_addr is not None and len(data) >= 3:
                seq, chunk, c = data[0], data[1:-1], data[-1]
                if seq != self.seq or crc8(chunk) != c:
                    self.send(bytes([0x21, 0xAA, 0x8A, 0x02, 0xE3, 0x00]))
                    return
                if a.corrupt_page is not None and self.prog_addr // PAGE_SIZE == a.corrupt_page:
                    chunk = bytes([chunk[0] ^ 0x01]) + bytes(chunk[1:])
                for i, b in enumerate(chunk):               # NOR flash: can only clear bits
                    self.flash[self.prog_addr + i] &= b
                self.prog_addr += len(chunk)
                self.seq = (self.seq + 1) & 0xFF
                self.send(bytes([0x21, 0xAA, 0x8A, 0x01, self.seq]))
            else:
                self.send(bytes([0x21, 0xAA, 0x8A, 0x01, 0xE4]))
        elif grp == 0x0F and cmd == 0x91:
            self.send(self.versions_frame(0x11, self.cur))
        elif grp == 0x0F and cmd == 0x92:
            self.send(self.versions_frame(0x12, self.stored_versions()))
        elif grp == 0x0F and cmd == 0x89:
            self.send(bytes([0x21, 0x0F, 0x09, 0x07, 0x00, 0x00, 0x00, 1, 2, 1, 0]))
        elif grp == 0x0F and cmd == 0x50 and len(data) == 1:
            self.start_update(data[0])
        elif grp == 0x0F and cmd == 0xAA:
            self.finish_update(0.5)

    def start_update(self, mask):
        a = self.args
        t = 0.1
        self.send(bytes([0x21, 0x0F, 0xD0, 0x00]), t)
        if a.finish == "progress":
            # What a real CXA61 was seen to do: 0%, a silent pause, 1..78%, then
            # 100%; a few seconds later it has restarted into standby.
            t += 0.25
            self.send(bytes([0x21, 0x0F, 0xD5, 0x01, 0]), t)
            t += a.reflash
            for pct in list(range(1, 79)) + [100]:
                t += a.step / 5
                self.send(bytes([0x21, 0x0F, 0xD5, 0x01, pct]), t)
            self.pending.append((time.monotonic() + t, "apply"))
            self.pending.append((time.monotonic() + t + 4.0, "standby"))
            return
        for pct in range(0, 101, 10):
            t += a.step
            self.send(bytes([0x21, 0x0F, 0xD7, 0x01, pct]), t)
        if a.finish == "reset":
            self.finish_update(t + a.reflash)

    def apply_store(self):
        self.cur.update(self.stored_versions())

    def finish_update(self, delay):
        self.pending.append((time.monotonic() + delay, None))      # None = apply + announce

    def feed(self, data):
        if self.capture:
            self.capture.write(data)
            self.capture.flush()
        self.rx += data
        while self.rx:
            b = self.rx[0]
            if b == 0x23:
                end = self.rx.find(b"\r")
                if end < 0:
                    return
                line = bytes(self.rx[:end])
                del self.rx[:end + 1]
                self.handle_text(line)
            elif b == 0x21:
                if len(self.rx) < 4:
                    return
                n = self.rx[3]
                if len(self.rx) < 4 + n:
                    return
                grp, cmd, data = self.rx[1], self.rx[2], bytes(self.rx[4:4 + n])
                del self.rx[:4 + n]
                self.handle_bin(grp, cmd, data)
            else:
                del self.rx[0]

    def due(self):
        now = time.monotonic()
        ready = [p for p in self.pending if p[0] <= now]
        self.pending = [p for p in self.pending if p[0] > now]
        for _, data in sorted(ready, key=lambda p: p[0]):
            if data is None:
                self.apply_store()
                self.out += b"#02,01,1\r"
            elif data == "apply":
                self.apply_store()
            elif data == "standby":
                self.on = False
                self.out += b"#02,01,0\r"
            else:
                self.out += data


def main():
    p = argparse.ArgumentParser(description="Pretend CXA61 on a pseudo-terminal.")
    p.add_argument("--model", default="CA0877",
                   help="product code to report: CA0877 (what a real CXA61 reports, default) or CA0837")
    p.add_argument("--mcu", type=int, nargs=2, default=[1, 2], metavar=("MAJOR", "MINOR"),
                   help="main MCU version the pretend amp starts with (default 1 2)")
    p.add_argument("--usb", type=int, nargs=2, default=[1, 1], metavar=("MAJOR", "MINOR"))
    p.add_argument("--standby", action="store_true", help="start in standby")
    p.add_argument("--max-block", type=int, default=128)
    p.add_argument("--finish", choices=["progress", "reset", "silent"], default="progress",
                   help="how the pretend amp ends the self-update: progress = percent reports up to "
                        "100 then restart into standby, as seen on a real CXA61 (default); "
                        "reset = only a power-state message after restarting; silent = never reports")
    p.add_argument("--step", type=float, default=0.15, help="seconds between progress reports")
    p.add_argument("--reflash", type=float, default=3.0, help="seconds of silence while 'reprogramming'")
    p.add_argument("--link", help="also create this symlink to the pseudo-terminal")
    p.add_argument("--capture", help="write every byte received from the host to this file")
    p.add_argument("--die-after", type=int, default=0, help="stop answering after N binary frames")
    p.add_argument("--corrupt-page", type=int, help="store this 4 KB page wrongly (to test verification)")
    p.add_argument("--preload", help="firmware .hex to pre-store in the flash (as if already downloaded)")
    args = p.parse_args()

    amp = FakeAmp(args)
    if args.preload:
        from cxa_update import Image
        img = Image(args.preload)
        amp.flash[:len(img.buf)] = img.buf

    master, slave = pty.openpty()
    tty.setraw(slave)
    attrs = termios.tcgetattr(slave)
    attrs[3] &= ~termios.ECHO
    termios.tcsetattr(slave, termios.TCSANOW, attrs)
    path = os.ttyname(slave)
    if args.link:
        if os.path.islink(args.link):
            os.unlink(args.link)
        os.symlink(path, args.link)
        path = args.link
    print("Pretend amplifier (%s) ready on: %s" % (args.model, path))
    print("  python3 cxa_update.py probe --port %s" % path)
    print("Ctrl-C to stop.")
    sys.stdout.flush()
    try:
        while True:
            r, _, _ = select.select([master], [], [], 0.02)
            if r:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    data = b""
                if data:
                    amp.feed(data)
            amp.due()
            if amp.out:
                os.write(master, bytes(amp.out))
                amp.out.clear()
    except KeyboardInterrupt:
        pass
    finally:
        if args.link and os.path.islink(args.link):
            os.unlink(args.link)


if __name__ == "__main__":
    main()
