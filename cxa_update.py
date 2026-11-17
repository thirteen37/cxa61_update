#!/usr/bin/env python3
"""
cxa_update.py - macOS / Linux port of Cambridge Audio's Windows-only
"CXAx1 Updater to MCU v4.0" (AP364281) for the CXA61 amplifier.

The logic here was ported from the vendor's .NET updater
(AP364281_Cambridge_CXAx1_Updater_to_MCU_v4.0.exe, "CXAx1 Upgrader v2.0"),
and the firmware it flashes is the unmodified image embedded in that program.
Give it either the vendor's updater .exe (the image is read straight out of it,
nothing in the .exe is executed) or an extracted .hex file.

Needs only Python 3.8+ (standard library, no pyserial) and a USB-to-RS232
cable plugged into the RS232 port on the back of the amplifier.

    python3 cxa_update.py info   "Updater.exe"   # offline: what is in this updater?
    python3 cxa_update.py ports                  # list candidate serial ports
    python3 cxa_update.py probe  "Updater.exe"   # find the amp and read its versions
    python3 cxa_update.py update "Updater.exe"   # do the update
    python3 cxa_update.py extract "Updater.exe"  # save the embedded image as a .hex

Without a file name it looks for the .hex / updater .exe next to this script.

A NEWER updater from Cambridge Audio: this script knows the update procedure of
"CXAx1 Upgrader v2.0".  It will read the image out of any updater .exe and check
it thoroughly, but it cannot know whether a newer updater also changed the
procedure.  It tells you what it found and asks for --allow-other-firmware
before using an image it has not seen.

What "update" does (same sequence as the vendor tool):

  1. Finds the amp ("#13,03" -> "#14,03,CA0837" or "...CA0877"), switches it
     on if it is in standby and selects source A1.
  2. Reads the versions currently running and those staged in the amp's
     internal SPI flash "download" store.
  3. Stage 1 - writes the image into that store, 4 KB page by page (pages whose
     CRC already matches are skipped), then asks the amp for the CRC of each
     stored image and compares it with the file.  Nothing the amp runs from is
     touched in this stage, so it can be interrupted and simply run again.
  4. Stage 2 - tells the amp to reprogram its MCU from the store.  The amp does
     this by itself (front panel: A1, PROTECTION, MUTE and D2 lit, SPEAKER A/B
     flashing), reporting progress as it goes, and then restarts into standby.
     DO NOT SWITCH THE AMP OFF DURING THIS STAGE.

Serial protocol summary (9600 8N1, no flow control)

  ASCII control protocol:  "#gg,nn[,data]\\r"
  Binary service protocol: 0x21 '!', group, command, length, data[length]
      group 0xAA = flash store access      group 0x0F = unit / update control
      !AA 8B 00                 -> !AA 0B 03 <JEDEC id>     flash chip id
      !AA 8A 04 a2 a1 a0 02     -> !AA 0A 05 a2 a1 a0 hi lo read 2 bytes:
            0x80pppp = CRC-16 of 4 KB page, 0x4000i0 = CRC-16 of stored image i
      !AA 0A 00                 -> !AA 8A 00                reset flash session
      !AA 0A 07 F5 addr32 20 c8 -> !AA 8A 00                erase 4 KB sector
      !AA 0A 07 F5 addr32 02 c8 -> !AA 8A 03 seq st maxblk  start page program
      !AA 0A n+2 seq data[n] c8 -> !AA 8A 01 nextseq        program data (n = maxblk, at most 128)
      !0F 91 00 / !0F 92 00     -> !0F 11 / !0F 12 ...      running / stored versions
      !0F 89 00                 -> !0F 09 07 ...            status, bootloader versions
      !0F 50 01 mask            -> !0F Dx 01 percent ...    update from store
                                   (mask: 01 = main MCU, 02 = front panel MCU, 20 = Bluetooth)
      !0F AA 00                                             reboot
  c8 is a CRC-8 (polynomial 0x07) of the bytes after the F5 / sequence byte.
"""

import argparse
import fcntl
import glob
import hashlib
import os
import re
import select
import sys
import termios
import time

TOOL = "cxa_update.py (port of Cambridge Audio CXAx1 Upgrader v2.0 / AP364281)"

FIRMWARE_NAME = "AP364281_CA0837_CA0877_Update.hex"
# Images this script has been checked against: SHA-256 of the embedded .hex -> description.
KNOWN_FIRMWARE = {
    "c71b97a74341829970d3ff8c27fc94b02794355eb354c072fb0feecfd40b675c":
        "AP364281 'CXAx1 Updater to MCU v4.0'",
}
# Version of the vendor updater program whose procedure this script follows.
KNOWN_UPGRADER = "2.0"

BUFFER_SIZE = 0x2C0000          # size of the vendor tool's image buffer
PAGE_SIZE = 4096                # flash sector the tool erases/programs at a time

# Image slots ("pages") in the amp's flash store, as defined in the vendor tool:
# index: (name, header identifier, data start, header address)
SLOTS = {
    0: ("USB Audio",          0x41, 0x000000, 0x00FFF0),
    3: ("Front panel MCU",    0x46, 0x030010, 0x030000),
    4: ("Main MCU",           0x44, 0x040000, 0x040000),
    5: ("Bluetooth",          0x45, 0x0C0010, 0x0C0000),
}
SLOT_ORDER = (0, 3, 4, 5)

# Table the vendor tool uses for its 16-bit CRC (copied verbatim from the program).
CRC16_TABLE = (
    0x0000, 0x1189, 0x2312, 0x329B, 0x4624, 0x57AD, 0x6536, 0x74BF,
    0x8C48, 0x9DC1, 0xAF5A, 0xBED3, 0xCA6C, 0xDBE5, 0xE97E, 0xF8F7,
    0x0919, 0x1890, 0x2A0B, 0x3B82, 0x4F3D, 0x5EB4, 0x6C2F, 0x7DA6,
    0x8551, 0x94D8, 0xA643, 0xB7CA, 0xC375, 0xD2FC, 0xE067, 0xF1EE,
    0x1232, 0x03BB, 0x3120, 0x20A9, 0x5416, 0x459F, 0x7704, 0x668D,
    0x9E7A, 0x8FF3, 0xBD68, 0xACE1, 0xD85E, 0xC9D7, 0xFB4C, 0xEAC5,
    0x1B2B, 0x0AA2, 0x3839, 0x29B0, 0x5D0F, 0x4C86, 0x7E1D, 0x6F94,
    0x9763, 0x86EA, 0xB471, 0xA5F8, 0xD147, 0xC0CE, 0xF255, 0xE3DC,
    0x2464, 0x35ED, 0x0776, 0x16FF, 0x6240, 0x73C9, 0x4152, 0x50DB,
    0xA82C, 0xB9A5, 0x8B3E, 0x9AB7, 0xEE08, 0xFF81, 0xCD1A, 0xDC93,
    0x2D7D, 0x3CF4, 0x0E6F, 0x1FE6, 0x6B59, 0x7AD0, 0x484B, 0x59C2,
    0xA135, 0xB0BC, 0x8227, 0x93AE, 0xE711, 0xF698, 0xC403, 0xD58A,
    0x3656, 0x27DF, 0x1544, 0x04CD, 0x7072, 0x61FB, 0x5360, 0x42E9,
    0xBA1E, 0xAB97, 0x990C, 0x8885, 0xFC3A, 0xEDB3, 0xDF28, 0xCEA1,
    0x3F4F, 0x2EC6, 0x1C5D, 0x0DD4, 0x796B, 0x68E2, 0x5A79, 0x4BF0,
    0xB307, 0xA28E, 0x9015, 0x819C, 0xF523, 0xE4AA, 0xD631, 0xC7B8,
    0x48C8, 0x5941, 0x6BDA, 0x7A53, 0x0EEC, 0x1F65, 0x2DFE, 0x3C77,
    0xC480, 0xD509, 0xE792, 0xF61B, 0x82A4, 0x932D, 0xA1B6, 0xB03F,
    0x41D1, 0x5058, 0x62C3, 0x734A, 0x07F5, 0x167C, 0x24E7, 0x356E,
    0xCD99, 0xDC10, 0xEE8B, 0xFF02, 0x8BBD, 0x9A34, 0xA8AF, 0xB926,
    0x5AFA, 0x4B73, 0x79E8, 0x6861, 0x1CDE, 0x0D57, 0x3FCC, 0x2E45,
    0xD6B2, 0xC73B, 0xF5A0, 0xE429, 0x9096, 0x811F, 0xB384, 0xA20D,
    0x53E3, 0x426A, 0x70F1, 0x6178, 0x15C7, 0x044E, 0x36D5, 0x275C,
    0xDFAB, 0xCE22, 0xFCB9, 0xED30, 0x998F, 0x8806, 0xBA9D, 0xAB14,
    0x6CAC, 0x7D25, 0x4FBE, 0x5E37, 0x2A88, 0x3B01, 0x099A, 0x1813,
    0xE0E4, 0xF16D, 0xC3F6, 0xD27F, 0xA6C0, 0xB749, 0x85D2, 0x945B,
    0x65B5, 0x743C, 0x46A7, 0x572E, 0x2391, 0x3218, 0x0083, 0x110A,
    0xE9FD, 0xF874, 0xCAEF, 0xDB66, 0xAFD9, 0xBE50, 0x8CCB, 0x9D42,
    0x7E9E, 0x6F17, 0x5D8C, 0x4C05, 0x38BA, 0x2933, 0x1BA8, 0x0A21,
    0xF2D6, 0xE35F, 0xD1C4, 0xC04D, 0xB4F2, 0xA57B, 0x97E0, 0x8669,
    0x7787, 0x660E, 0x5495, 0x451C, 0x31A3, 0x202A, 0x12B1, 0x0338,
    0xFBCF, 0xEA46, 0xD8DD, 0xC954, 0xBDEB, 0xAC62, 0x9EF9, 0x8F70,
)


class UpdateError(Exception):
    """Something went wrong; the message says what and what to do next."""


# --------------------------------------------------------------------------
# Checksums (literal ports of the vendor routines)
# --------------------------------------------------------------------------

def crc16(data, offset=0, length=None):
    """CalculateCRC16(): init 0xFFFF, MSB-first update with the table above."""
    if length is None:
        length = len(data) - offset
    crc = 0xFFFF
    table = CRC16_TABLE
    for b in memoryview(data)[offset:offset + length]:
        crc = ((crc << 8) & 0xFFFF) ^ table[(crc >> 8) ^ b]
    return crc


def crc8(data):
    """CrcCalculate(): bitwise CRC-8, polynomial 0x07, initial value 0."""
    n = len(data)
    if n == 0:
        return 0
    v = data[0] << 8
    i = 1
    while i <= n:
        if i != n:
            v |= data[i]
        for _ in range(8):
            v <<= 1
            if v & 0x10000:
                v ^= 0x0700
        v &= 0xFFFFFFFF
        i += 1
    return (v >> 8) & 0xFF


def checksum8(data, offset, length):
    """CheckSum(): two's-complement of the byte sum."""
    return (-sum(memoryview(data)[offset:offset + length])) & 0xFF


# --------------------------------------------------------------------------
# Firmware image
# --------------------------------------------------------------------------

_HEX_LINE = re.compile(
    r"^:([0-1][0-9A-Fa-f]|20)([0-9A-Fa-f]{4})0([0-5])(([0-9A-Fa-f]{2})*)([0-9A-Fa-f]{2})$")


class Slot:
    def __init__(self, index):
        self.index = index
        self.name, self.ident, self.start, self.header = SLOTS[index]
        self.present = False        # valid image for this slot in the file
        self.damaged = False        # header says there is one, but it fails its checks
        self.size = 0
        self.major = 0
        self.minor = 0
        self.crc = 0
        # What the amp reports:
        self.cur_valid = False
        self.cur = (0, 0)           # version currently running
        self.store_valid = False
        self.store = (0, 0)         # version staged in the flash store

    def file_version(self):
        return fmt_version(self.index, (self.major, self.minor))


def fmt_version(index, ver):
    major, minor = ver
    if index == 5:                  # Bluetooth is shown as x.y.zz by the vendor tool
        return "%d.%d.%02d" % ((major >> 4) & 15, major & 15, minor)
    return "%d.%d" % (major, minor)


_HEX_RUN = re.compile(rb"(?::[0-9A-Fa-f]{10,522}\r?\n)+")
_HEX_EOF = re.compile(rb":00000001FF\s*$")


def _dotnet_resources(data):
    """Return the managed-resource blob of a .NET executable, or None."""
    try:
        if data[:2] != b"MZ":
            return None
        pe = int.from_bytes(data[0x3C:0x40], "little")
        if data[pe:pe + 4] != b"PE\0\0":
            return None
        nsect = int.from_bytes(data[pe + 6:pe + 8], "little")
        optsize = int.from_bytes(data[pe + 20:pe + 22], "little")
        opt = pe + 24
        magic = int.from_bytes(data[opt:opt + 2], "little")
        dirs = opt + (96 if magic == 0x10B else 112)
        sections = []
        for i in range(nsect):
            sh = opt + optsize + 40 * i
            vsize, vaddr, rsize, rptr = (int.from_bytes(data[sh + o:sh + o + 4], "little")
                                         for o in (8, 12, 16, 20))
            sections.append((vaddr, max(vsize, rsize), rptr))

        def offset(rva):
            for vaddr, size, rptr in sections:
                if vaddr <= rva < vaddr + size:
                    return rva - vaddr + rptr
            return None

        cli_rva = int.from_bytes(data[dirs + 14 * 8:dirs + 14 * 8 + 4], "little")
        cli = offset(cli_rva) if cli_rva else None
        if cli is None:
            return None
        res_rva = int.from_bytes(data[cli + 24:cli + 28], "little")
        res_size = int.from_bytes(data[cli + 28:cli + 32], "little")
        start = offset(res_rva) if res_rva else None
        if start is None or res_size == 0:
            return None
        return data[start:start + res_size]
    except (IndexError, ValueError):
        return None


def extract_hex_from_exe(data):
    """Find the Intel HEX firmware image embedded in an updater .exe.

    The vendor program carries the image as a plain-text managed resource
    (4-byte length, then the text).  Returns (hex_bytes, how_found)."""
    blob = _dotnet_resources(data)
    where = "embedded .NET resource"
    if blob is None:
        blob, where = data, "text found inside the file"
    found = []
    for m in _HEX_RUN.finditer(blob):
        text = m.group(0)
        if len(text) < 1024:
            continue
        # Prefer the resource's own length field when it brackets this text exactly.
        if m.start() >= 4:
            n = int.from_bytes(blob[m.start() - 4:m.start()], "little")
            if m.start() + n >= m.end() - 2 and m.start() + n <= len(blob) and n < m.end() - m.start() + 16:
                text = blob[m.start():m.start() + n]
        if _HEX_EOF.search(text):
            found.append(text)
    if not found:
        raise UpdateError("No firmware image found inside this file. Is it a Cambridge Audio "
                          "CXA61 updater program?")
    if len(found) > 1:
        raise UpdateError("This updater contains %d separate firmware images; this script only "
                          "knows the single-image kind. Not using it." % len(found))
    return found[0], where


def upgrader_version(data):
    """Version label of the vendor updater program (e.g. '2.0'), if it can be found."""
    m = re.search(re.escape("CXAx1 Upgrader v".encode("utf-16-le")) + rb"((?:[0-9.]\x00)+)", data)
    return m.group(1).decode("utf-16-le") if m else None


def read_firmware_file(path):
    """Read a .hex file or an updater .exe.  Returns (hex_bytes, source, upgrader_version)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        raise UpdateError("Cannot read %s: %s" % (path, e.strerror))
    if data[:2] == b"MZ":
        text, where = extract_hex_from_exe(data)
        return text, "updater program (%s)" % where, upgrader_version(data)
    return data, "hex file", None


class Image:
    """The update file, loaded the way HexReaderResource/loadCompactImage do."""

    def __init__(self, path):
        self.path = path
        raw, self.source, self.upgrader = read_firmware_file(path)
        self.hex_text = raw
        self.is_exe = self.source != "hex file"
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.buf = bytearray(b"\xFF" * BUFFER_SIZE)
        self.stored_crcs = {}       # CRC records the file carries at 0x4000i0
        self._beyond = {}           # bytes addressed outside the buffer
        self._parse(raw.decode("latin-1"))
        self.slots = {i: Slot(i) for i in SLOT_ORDER}
        for slot in self.slots.values():
            self._read_slot(slot)

    def _parse(self, text):
        upper = 0
        records = 0
        for line in text.splitlines():
            m = _HEX_LINE.match(line)
            if not m:
                continue
            if sum(bytes.fromhex(line[1:])) & 0xFF:
                continue            # bad record checksum: the vendor tool skips it too
            rtype = m.group(3)
            if rtype == "0":
                addr = (upper & 0xFFFF0000) | int(m.group(2), 16)
                data = bytes.fromhex(m.group(4))
                for i, b in enumerate(data):
                    a = addr + i
                    if a < BUFFER_SIZE:
                        self.buf[a] = b
                    else:
                        self._beyond[a] = b     # the vendor tool ignores these
                records += 1
            elif rtype == "4":
                if m.group(1) == "02" and m.group(2) == "0000":
                    upper = int(m.group(4), 16) << 16
        if records == 0:
            raise UpdateError("%s does not look like an Intel HEX firmware file." % self.path)
        for i in SLOT_ORDER:
            a = 0x400000 + (i << 4)
            if a in self._beyond and a + 1 in self._beyond:
                self.stored_crcs[i] = (self._beyond[a] << 8) | self._beyond[a + 1]

    def _read_slot(self, s):
        b = self.buf
        h = s.header
        if b[h + 1] != s.ident:
            return
        s.damaged = True            # cleared below once everything checks out
        size = int.from_bytes(b[h + 5:h + 9], "big")
        if size <= 0 or s.start + size > BUFFER_SIZE:
            return
        if s.start == s.header:
            calc = checksum8(b, s.start + 16, size - 16)
        else:
            calc = checksum8(b, s.start, size)
        if b[h + 2] != calc:
            return
        s.damaged = False
        s.size = size
        s.major = b[h + 3]
        s.minor = b[h + 4]
        s.crc = crc16(b, s.start, size)
        s.present = True

    def present_slots(self):
        return [self.slots[i] for i in SLOT_ORDER if self.slots[i].present]

    def pages_to_stage(self, flash_size):
        """4 KB page numbers command_doFlashDownload() marks for a single image file."""
        pages = []
        for i in SLOT_ORDER:
            s = self.slots[i]
            if not s.present:
                continue
            if i == 0:
                pages.extend(range(0, 16))          # whole first 64 KB
                continue
            if i == 5 and flash_size <= 0x80000:
                raise UpdateError("The file contains a Bluetooth image but this unit's "
                                  "flash store is too small for it.")
            length = s.size if s.start == s.header else s.size + 16
            first = (s.start >> 16) * 16
            count = (length + PAGE_SIZE - 1) // PAGE_SIZE
            pages.extend(range(first, first + count))
        return sorted(set(pages))


def find_firmware(arg):
    if arg:
        return arg
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (here, os.getcwd()):
        p = os.path.join(d, FIRMWARE_NAME)
        if os.path.exists(p):
            return p
    exes = []
    for d in (here, os.getcwd()):
        for p in sorted(glob.glob(os.path.join(d, "*.exe"))):
            if "cxa" in os.path.basename(p).lower() and p not in exes:
                exes.append(p)
    if len(exes) == 1:
        return exes[0]
    if len(exes) > 1:
        raise UpdateError("Several updater programs found here - say which one to use:\n  "
                          + "\n  ".join(exes))
    raise UpdateError("No firmware given. Pass the vendor's updater .exe (or a .hex file), e.g.\n"
                      "  python3 cxa_update.py update \"AP364281 Cambridge CXAx1 Updater to MCU v4.0.exe\"")


def load_image(args):
    path = find_firmware(args.firmware)
    image = Image(path)
    for i in SLOT_ORDER:
        s = image.slots[i]
        if s.damaged or (i in image.stored_crcs and not s.present):
            raise UpdateError("Invalid upgrade file: the '%s' image in %s is damaged or "
                              "incomplete (its checksum does not match)." % (s.name, path))
    if not image.present_slots():
        raise UpdateError("Invalid upgrade file: no valid image found in %s." % path)
    # Integrity checks that hold for any genuine image, known or not.
    for i, crc in image.stored_crcs.items():
        s = image.slots[i]
        if s.present and s.crc != crc:
            raise UpdateError("Invalid upgrade file: CRC recorded in the file for '%s' "
                              "(%04X) does not match its contents (%04X)."
                              % (s.name, crc, s.crc))
    if image.sha256 in KNOWN_FIRMWARE:
        return image
    if not args.allow_other_firmware:
        describe_image(image)
        problems = unknown_image_notes(image)
        raise UpdateError(
            "This is not an image this script has been checked against.\n"
            + "\n".join("  - " + p for p in problems) +
            "\nIf you have decided to go ahead, repeat the command with --allow-other-firmware.")
    return image


def unknown_image_notes(image):
    notes = []
    if image.is_exe:
        if image.upgrader is None:
            notes.append("The updater program does not identify itself as 'CXAx1 Upgrader'; "
                         "it may be for a different product or work differently.")
        elif image.upgrader != KNOWN_UPGRADER:
            notes.append("The updater program is 'CXAx1 Upgrader v%s'; this script follows the "
                         "procedure of v%s, and the newer program may do things differently."
                         % (image.upgrader, KNOWN_UPGRADER))
        else:
            notes.append("The updater program is the same 'CXAx1 Upgrader v%s' this script follows, "
                         "with a different image inside - a good sign, but not proof that its "
                         "procedure is unchanged." % image.upgrader)
    else:
        notes.append("A bare .hex file says nothing about the updater program it came from.")
    missing = [s.name for s in image.present_slots() if s.index not in image.stored_crcs]
    if missing:
        notes.append("The file carries no CRC record for: %s." % ", ".join(missing))
    else:
        notes.append("The image itself is internally consistent (header checksums and CRC records match).")
    return notes


def describe_image(image):
    print("Firmware from : %s  [%s]" % (image.path, image.source))
    if image.is_exe:
        print("Updater       : CXAx1 Upgrader v%s" % image.upgrader if image.upgrader
              else "Updater       : (program does not identify itself as CXAx1 Upgrader)")
    known = KNOWN_FIRMWARE.get(image.sha256)
    print("Image SHA-256 : %s" % image.sha256)
    print("                %s" % ("known image: " + known if known else "NOT an image this script has been checked against"))
    for s in image.present_slots():
        print("  %-16s version %-6s %7d bytes at 0x%06X, CRC-16 %04X"
              % (s.name, s.file_version(), s.size, s.start, s.crc))
    missing = [SLOTS[i][0] for i in SLOT_ORDER if not image.slots[i].present]
    if missing:
        print("  (not in this file: %s)" % ", ".join(missing))


# --------------------------------------------------------------------------
# Serial port (plain termios, so no third-party module is needed)
# --------------------------------------------------------------------------

class SerialPort:
    def __init__(self, path, baud):
        speeds = {9600: termios.B9600, 115200: termios.B115200}
        if baud not in speeds:
            raise UpdateError("Unsupported speed %s (use 9600 or 115200)." % baud)
        self.path = path
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as e:
            raise UpdateError("Cannot open %s: %s" % (path, e.strerror))
        try:
            try:
                fcntl.ioctl(self.fd, termios.TIOCEXCL)
            except OSError:
                pass
            attrs = termios.tcgetattr(self.fd)
            attrs[0] = 0                                    # iflag: raw, no flow control
            attrs[1] = 0                                    # oflag
            cflag = attrs[2]
            cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB)
            if hasattr(termios, "CRTSCTS"):
                cflag &= ~termios.CRTSCTS
            cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[2] = cflag
            attrs[3] = 0                                    # lflag
            attrs[4] = speeds[baud]
            attrs[5] = speeds[baud]
            attrs[6][termios.VMIN] = 0
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            # The vendor tool leaves DTR and RTS de-asserted (.NET defaults).
            for line in (termios.TIOCM_DTR, termios.TIOCM_RTS):
                try:
                    fcntl.ioctl(self.fd, termios.TIOCMBIC, line.to_bytes(4, sys.byteorder))
                except OSError:
                    pass
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except Exception:
            os.close(self.fd)
            raise

    def write(self, data):
        view = memoryview(data)
        deadline = time.monotonic() + 5.0
        while view:
            left = deadline - time.monotonic()
            if left <= 0:
                raise UpdateError("Timed out writing to %s." % self.path)
            _, w, _ = select.select([], [self.fd], [], left)
            if not w:
                continue
            try:
                n = os.write(self.fd, view)
            except BlockingIOError:
                continue
            except OSError as e:
                raise UpdateError("Write to %s failed: %s (cable unplugged?)" % (self.path, e.strerror))
            view = view[n:]

    def read(self, timeout):
        """Return whatever arrives within timeout seconds (b'' if nothing)."""
        r, _, _ = select.select([self.fd], [], [], max(0.0, timeout))
        if not r:
            return b""
        try:
            data = os.read(self.fd, 256)
        except BlockingIOError:
            return b""
        except OSError as e:
            raise UpdateError("Read from %s failed: %s (cable unplugged?)" % (self.path, e.strerror))
        if not data:
            time.sleep(0.01)
        return data

    def flush_input(self):
        try:
            termios.tcflush(self.fd, termios.TCIFLUSH)
        except termios.error:
            pass

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None


def candidate_ports():
    patterns = ["/dev/cu.usbserial*", "/dev/cu.SLAB_USBtoUART*", "/dev/cu.wchusbserial*",
                "/dev/cu.PL2303*", "/dev/cu.usbmodem*", "/dev/ttyUSB*"]
    seen = []
    for pat in patterns:
        for p in sorted(glob.glob(pat)):
            if p not in seen:
                seen.append(p)
    return seen


# --------------------------------------------------------------------------
# Link: framing, receive parser (same state machine as pPort_DataReceived)
# --------------------------------------------------------------------------

class Link:
    def __init__(self, port, log):
        self.port = port
        self.log = log
        self.state = 0
        self.buf = bytearray()
        self.remaining = 0
        self.queue = []

    def send(self, frame, note=""):
        frame = bytes(frame)
        self.log.io("TX", frame, note)
        self.port.write(frame)

    def _feed(self, data):
        for b in data:
            st = self.state
            if st == 0:
                if b == 0x21:
                    self.buf = bytearray()
                    self.state = 1
                elif b == 0x23:
                    self.buf = bytearray(b"#")
                    self.state = 5
            elif st == 1:
                self.buf.append(b)
                self.state = 2
            elif st == 2:
                self.buf.append(b)
                self.state = 3
            elif st == 3:
                self.buf.append(b)
                if b > 30:
                    self.state = 0
                elif b == 0:
                    self.state = 0
                    self.queue.append(("bin", bytes(self.buf)))
                else:
                    self.remaining = b
                    self.state = 4
            elif st == 4:
                self.buf.append(b)
                self.remaining -= 1
                if self.remaining == 0:
                    self.state = 0
                    self.queue.append(("bin", bytes(self.buf)))
            elif st == 5:
                self.buf.append(b)
                if b == 0x0D:
                    self.state = 0
                    self.queue.append(("txt", bytes(self.buf)))
                elif len(self.buf) >= 32:
                    self.state = 0

    def next_frame(self, deadline):
        """Next complete frame as (kind, bytes), or None when the deadline passes."""
        while True:
            if self.queue:
                kind, frame = self.queue.pop(0)
                self.log.io("RX", frame, "")
                return kind, frame
            left = deadline - time.monotonic()
            if left <= 0:
                return None
            data = self.port.read(min(left, 0.25))
            if data:
                self._feed(data)

    def wait(self, accept, timeout, what, on_text=None):
        """Wait for a binary frame for which accept(frame) is true."""
        deadline = time.monotonic() + timeout
        while True:
            item = self.next_frame(deadline)
            if item is None:
                raise UpdateError("No answer from the amplifier while %s "
                                  "(waited %.0f s)." % (what, timeout))
            kind, frame = item
            if kind == "txt":
                if on_text:
                    on_text(frame)
                continue
            if accept(frame):
                return frame

    def reset_parser(self):
        self.state = 0
        self.queue = []


class Log:
    def __init__(self, path, verbose):
        self.verbose = verbose
        self.file = open(path, "w") if path else None
        self.t0 = time.monotonic()

    def io(self, direction, frame, note):
        if not (self.file or self.verbose):
            return
        if frame[:1] == b"#":
            body = repr(frame.decode("latin-1"))
        else:
            body = frame.hex(" ").upper()
        line = "%9.3f %s %s%s" % (time.monotonic() - self.t0, direction, body,
                                  ("   ; " + note) if note else "")
        if self.file:
            self.file.write(line + "\n")
        if self.verbose:
            print("    " + line)

    def note(self, text):
        if self.file:
            self.file.write("%9.3f -- %s\n" % (time.monotonic() - self.t0, text))
            self.file.flush()

    def close(self):
        if self.file:
            self.file.close()


def say(log, text=""):
    print(text)
    sys.stdout.flush()
    if text:
        log.note(text)


# --------------------------------------------------------------------------
# ASCII control protocol: find the unit, power, source
# --------------------------------------------------------------------------

def _read_text(port, pattern, timeout):
    """Collect bytes for up to timeout seconds; return the match of pattern or None."""
    deadline = time.monotonic() + timeout
    data = b""
    while True:
        m = re.search(pattern, data)
        if m:
            return m
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        data += port.read(min(left, 0.1))


def identify(port, log):
    """SearchForUnit(): '#13,03' must be answered with '#14,03,CA0837' or '...CA0877'."""
    port.flush_input()
    log.io("TX", b"#13,03\r", "who are you?")
    port.write(b"#13,03\r")
    m = _read_text(port, rb"#14,03,(CA08[37]7)", 1.5)
    if not m:
        return None
    log.io("RX", m.group(0) + b"\r", "")
    return m.group(1).decode()


# Product codes the vendor updater accepts.  A real CXA61 reports CA0877; which
# model reports CA0837 is not known.
MODEL_NAMES = {"CA0877": "CXA61"}


def open_and_identify(args, log):
    if args.port:
        ports = [args.port]
    else:
        ports = [p for p in candidate_ports() if "usbmodem" not in p]
        if not ports:
            raise UpdateError(
                "No USB serial cable found (looked for /dev/cu.usbserial* and similar).\n"
                "Plug the USB-to-RS232 cable in, run 'ports' to list what is there, "
                "or name the device with --port.")
    for path in ports:
        try:
            port = SerialPort(path, args.baud)
        except UpdateError as e:
            say(log, "  %s: %s" % (path, e))
            continue
        code = identify(port, log)
        if code:
            say(log, "Found %s (%s) on %s at %d baud."
                % (MODEL_NAMES.get(code, "amplifier"), code, path, args.baud))
            return port, code
        port.close()
        say(log, "  %s: no amplifier answered." % path)
    raise UpdateError(
        "Could not detect a CXA61 on any serial port.\n"
        "Check that the amp has mains power, that the cable is in its RS232 socket, and\n"
        "that it is a straight-through cable (a null-modem / crossed cable will not work).")


def power_on(port, log, allow_switch_on=True):
    """Make sure the unit is on, as the vendor tool does before talking binary."""
    log.io("TX", b"#01,01\r", "power state?")
    port.write(b"#01,01\r")
    m = _read_text(port, rb"#02,01,([01])", 5.0)
    if m:
        log.io("RX", m.group(0) + b"\r", "")
    if m and m.group(1) == b"1":
        return True
    if not allow_switch_on:
        return False
    say(log, "Amplifier is in standby - switching it on.")
    log.io("TX", b"#01,02,1\r", "power on")
    port.write(b"#01,02,1\r")
    m = _read_text(port, rb"#02,03,0", 8.0)       # the unit reports 'mute off' once it is up
    if m:
        log.io("RX", m.group(0) + b"\r", "")
        return True
    # Not the answer the vendor tool waits for; ask again before giving up.
    log.io("TX", b"#01,01\r", "power state?")
    port.write(b"#01,01\r")
    m = _read_text(port, rb"#02,01,1", 5.0)
    return bool(m)


# --------------------------------------------------------------------------
# Binary service protocol
# --------------------------------------------------------------------------

F_GET_FLASH_ID = bytes([0x21, 0xAA, 0x8B, 0x00])
F_FLASH_RESET = bytes([0x21, 0xAA, 0x0A, 0x00])
F_REBOOT = bytes([0x21, 0x0F, 0xAA, 0x00])

T_REPLY = 10.0        # ordinary request/answer
T_VERIFY = 60.0       # amp computing the CRC of a whole stored image
T_UPDATE = 900.0      # amp reprogramming itself


def is_aa(sub):
    return lambda f: f[0] == 0xAA and f[1] == sub


def flash_command(addr, opcode):
    frame = bytearray([0x21, 0xAA, 0x0A, 0x07, 0xF5,
                       (addr >> 24) & 0xFF, (addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF,
                       opcode, 0x00])
    frame[10] = crc8(frame[5:10])
    return bytes(frame)


def data_frame(seq, chunk, limit):
    """One block of page data.  The vendor tool always transmits its whole 70- or
    134-byte frame buffer, so a short final block is followed by zero padding."""
    frame = bytes([0x21, 0xAA, 0x0A, len(chunk) + 2, seq]) + bytes(chunk) + bytes([crc8(chunk)])
    return frame.ljust(limit + 6, b"\x00")


def read_crc_frame(a2, a1, a0):
    return bytes([0x21, 0xAA, 0x8A, 0x04, a2, a1, a0, 0x02])


class Amp:
    def __init__(self, link, image, log):
        self.link = link
        self.image = image
        self.log = log
        self.flash_size = 0x80000
        self.flash_id = b""
        self.boot = None

    # -- GetVersions(): flash id, running versions, stored versions, status --
    def get_versions(self, attempts=1):
        last = None
        for attempt in range(attempts):
            try:
                return self._get_versions()
            except UpdateError as e:
                last = e
                self.link.reset_parser()
        raise last

    def _get_versions(self):
        link = self.link
        link.send(F_GET_FLASH_ID, "flash id?")
        f = link.wait(lambda f: f[0] == 0xAA and f[1] == 0x0B and f[2] == 3,
                      T_REPLY, "asking for the flash chip id")
        self.flash_id = bytes(f[3:6])
        if self.flash_id in (b"\xEF\x40\x16", b"\x20\xBA\x16"):
            self.flash_size = 0x400000
        else:
            self.flash_size = 0x80000
        for sub in (0x91, 0x92, 0x89):
            link.send(bytes([0x21, 0x0F, sub, 0x00]),
                      {0x91: "running versions?", 0x92: "stored versions?", 0x89: "status?"}[sub])
        got = {}

        def accept(f):
            if f[0] == 0x0F and f[1] in (0x11, 0x12, 0x09):
                got[f[1]] = f
            return f[0] == 0x0F and f[1] == 0x09 and f[2] == 7

        link.wait(accept, T_REPLY, "reading the version information")
        if 0x11 not in got or 0x12 not in got:
            raise UpdateError("The amplifier did not report its versions.")
        self._store_versions(got[0x11], "cur")
        self._store_versions(got[0x12], "store")
        st = got[0x09]
        self.boot = None if (st[3] & 1) else ((st[6], st[7]), (st[8], st[9]))

    def _store_versions(self, f, which):
        if f[2] < 7:
            raise UpdateError("Unexpected version answer from the amplifier: %s" % f.hex(" "))
        mask = f[3]
        layout = ((4, 0x01, 4), (3, 0x02, 6), (0, 0x04, 8), (5, 0x20, 14))
        for index, bit, pos in layout:
            s = self.image.slots[index]
            ok = not (mask & bit) and len(f) > pos + 1
            ver = (f[pos], f[pos + 1]) if ok else (0, 0)
            if which == "cur":
                s.cur_valid, s.cur = ok, ver
            else:
                s.store_valid, s.store = ok, ver

    def print_versions(self):
        log = self.log
        fid = self.flash_id.hex().upper()
        say(log, "  Flash store   : chip id %s, %s" % (fid, "4 MB" if self.flash_size > 0x80000 else "512 KB"))
        say(log, "  %-16s %-9s %-9s %s" % ("", "running", "stored", "this file"))
        for i in (4, 0, 3, 5):
            s = self.image.slots[i]
            cur = fmt_version(i, s.cur) if s.cur_valid else "-"
            sto = fmt_version(i, s.store) if s.store_valid else "-"
            fil = s.file_version() if s.present else "-"
            if cur == "-" and sto == "-" and fil == "-":
                continue
            say(log, "  %-16s %-9s %-9s %s" % (s.name, cur, sto, fil))
        if self.boot:
            say(log, "  Bootloaders   : main %d.%d, front panel %d.%d"
                % (self.boot[0][0], self.boot[0][1], self.boot[1][0], self.boot[1][1]))

    def up_to_date(self):
        return all(s.cur_valid and s.cur == (s.major, s.minor) for s in self.image.present_slots())

    # -- csPreProgramVerify: which pages already hold the right data? --
    def pages_needing_write(self, pages):
        link = self.link
        buf = self.image.buf
        todo = []
        for n, page in enumerate(pages, 1):
            a2 = ((page >> 4) & 0x3F) + 0x80
            a1 = (page << 4) & 0xF0
            link.send(read_crc_frame(a2, a1, 0x00), "CRC of page %d?" % page)
            f = link.wait(lambda f: f[0] == 0xAA and f[1] not in (0x11, 0x0B),
                          T_REPLY, "checking page %d of the flash store" % page)
            same = (f[1] == 0x0A and f[2] == 5 and (f[3] & 0xC0) == 0x80
                    and f[3] == a2 and f[4] == a1
                    and ((f[6] << 8) | f[7]) == crc16(buf, page * PAGE_SIZE, PAGE_SIZE))
            if not same:
                todo.append(page)
            progress("Checking store", n, len(pages))
        progress_done()
        return todo

    # -- csUpgradeFlash*: erase + program each page --
    def write_pages(self, pages):
        link = self.link
        buf = self.image.buf
        total = len(pages) * PAGE_SIZE
        done = 0
        started = time.monotonic()
        link.send(F_FLASH_RESET, "reset flash session")
        self._expect_ack("starting the download")
        for n, page in enumerate(pages):
            base = page * PAGE_SIZE
            link.send(flash_command(base, 0x20), "erase page %d" % page)
            self._expect_ack("erasing page %d" % page)
            link.send(flash_command(base, 0x02), "program page %d" % page)
            f = link.wait(is_aa(0x8A), T_REPLY, "starting to program page %d" % page)
            if f[2] != 3 or f[4] != 0:
                raise UpdateError("Error (0x%02X) occurred during Upgrade, page %d." % (f[3] if len(f) > 3 else 0, page))
            seq = f[3]
            max_block = f[5]
            if max_block == 0:
                raise UpdateError("The amplifier reported a block size of 0.")
            limit = 128 if max_block > 64 else 64
            offset = 0
            while offset < PAGE_SIZE:
                size = min(max_block, limit, PAGE_SIZE - offset)
                link.send(data_frame(seq, buf[base + offset:base + offset + size], limit))
                offset += size
                f = link.wait(is_aa(0x8A), T_REPLY, "programming page %d" % page)
                if f[2] != 1:
                    raise UpdateError("Error (0x%02X) occurred during Upgrade, page %d."
                                      % (f[3] if len(f) > 3 else 0, page))
                seq = f[3]
                done += size
                progress("Stage 1/2  writing store", done, total, started)
            if n != len(pages) - 1:
                link.send(F_FLASH_RESET, "reset flash session")
                self._expect_ack("finishing page %d" % page)
        progress_done()

    def _expect_ack(self, what):
        f = self.link.wait(is_aa(0x8A), T_REPLY, what)
        if f[2] != 0:
            raise UpdateError("Error (0x%02X) occurred during Upgrade Erase while %s." % (f[3], what))

    # -- csVerifyFlash: the amp's CRC of every stored image must equal the file's --
    def verify_store(self):
        link = self.link
        for s in self.image.present_slots():
            link.send(read_crc_frame(0x40, 0x00, (s.index << 4) & 0xFF), "CRC of stored %s?" % s.name)
            while True:
                f = link.wait(lambda f: f[0] == 0xAA and f[1] in (0x0A, 0x0F),
                              T_VERIFY, "verifying the stored %s image" % s.name)
                if f[1] == 0x0F:
                    raise UpdateError("CRC Check Error occurred during Download (%s)." % s.name)
                if f[2] == 5 and f[3] == 0x40:
                    break
            crc = (f[6] << 8) | f[7]
            if (f[5] >> 4) != s.index or crc != s.crc:
                raise UpdateError("CRC Check Error %04X occurred during Download: the stored %s "
                                  "image does not match the file (%04X). The amp has NOT been "
                                  "reprogrammed. Run the update again." % (crc, s.name, s.crc))
            say(self.log, "  Stored %s image verified by the amp (CRC %04X)." % (s.name, crc))

    # -- Command_doUpdate(): what has to be reprogrammed from the store? --
    def plan(self):
        def differs(i):
            s = self.image.slots[i]
            return s.present and s.store != s.cur
        return {"usb": differs(0), "bt": differs(5), "fpc": differs(3), "mcu": differs(4)}

    def start_update(self, plan):
        link = self.link
        mask = (0x02 if plan["fpc"] else 0) | (0x20 if plan["bt"] else 0) | (0x01 if plan["mcu"] else 0)
        link.send(F_FLASH_RESET, "reset flash session")
        f = link.wait(lambda f: is_aa(0x8A)(f) and f[2] == 0, T_REPLY, "preparing the unit update")
        link.send(bytes([0x21, 0x0F, 0x50, 0x01, mask]), "update from store, mask %02X" % mask)

    def reboot(self):
        self.link.send(F_REBOOT, "reboot")

    def watch_update(self, plan):
        """Follow the amp while it reprograms itself; return when it reports completion."""
        link = self.link
        log = self.log
        deadline = time.monotonic() + T_UPDATE
        last_rx = time.monotonic()
        last_pct = None
        waiting_reset = False
        announced_quiet = False
        while True:
            item = link.next_frame(min(deadline, time.monotonic() + 1.0))
            now = time.monotonic()
            if item is None:
                if now >= deadline:
                    return False
                if now - last_rx > 5 and not announced_quiet:
                    progress_done()
                    say(log, "  Amp is busy reprogramming (it sends nothing while it does) - keep waiting...")
                    announced_quiet = True
                continue
            last_rx = now
            kind, f = item
            if kind == "txt":
                # '#02,01,x' = power state message the unit sends when it has restarted.
                if f[1:3] == b"02" and f[4:6] == b"01":
                    progress_done()
                    return True
                continue
            if f[0] != 0x0F or f[2] != 1 or f[1] not in (0xD5, 0xD7, 0xD8):
                continue
            pct = f[3]
            if pct != last_pct:
                label = {0xD5: "Updating MCU", 0xD7: "Preparing update", 0xD8: "Updating Bluetooth"}[f[1]]
                progress("Stage 2/2  " + label, min(pct, 100), 100)
                last_pct = pct
            if pct != 100:
                continue
            if f[1] in (0xD5, 0xD8):
                progress_done()
                return True
            # 0xD7 complete
            if not plan["fpc"] and not plan["mcu"]:
                progress_done()
                self.reboot()
                return True
            if not waiting_reset:
                progress_done()
                say(log, "  Updating MCUs - PLEASE WAIT FOR UNIT RESET")
                waiting_reset = True


# --------------------------------------------------------------------------
# Progress display
# --------------------------------------------------------------------------

_progress_open = False


def progress(label, done, total, started=None):
    global _progress_open
    if not sys.stdout.isatty():
        return
    width = 28
    frac = 0 if total == 0 else min(1.0, done / total)
    bar = "#" * int(frac * width)
    extra = ""
    if started and done:
        elapsed = time.monotonic() - started
        remaining = elapsed * (total - done) / done
        extra = "  %d:%02d left" % (remaining // 60, remaining % 60)
    sys.stdout.write("\r  %-26s [%-*s] %3d%%%s   " % (label, width, bar, frac * 100, extra))
    sys.stdout.flush()
    _progress_open = True


def progress_done():
    global _progress_open
    if _progress_open:
        sys.stdout.write("\n")
        sys.stdout.flush()
        _progress_open = False


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_info(args, log):
    image = load_image(args)
    describe_image(image)
    return 0


def cmd_extract(args, log):
    image = load_image(args)
    describe_image(image)
    out = args.output or FIRMWARE_NAME
    if os.path.exists(out):
        raise UpdateError("%s already exists - not overwriting it (use --output NAME)." % out)
    with open(out, "wb") as f:
        f.write(image.hex_text)
    print("Saved the image to %s" % out)
    return 0


def cmd_ports(args, log):
    ports = candidate_ports()
    if not ports:
        print("No USB serial devices found. Plug the USB-to-RS232 cable in and try again.")
        return 1
    for p in ports:
        print(p)
    return 0


def connect(args, log, image, select_source):
    port, code = open_and_identify(args, log)
    try:
        if not power_on(port, log):
            raise UpdateError("The amplifier did not confirm that it is switched on. "
                              "Switch it on with its front panel button and try again.")
        if select_source:
            log.io("TX", b"#03,04,00\r", "select source A1")
            port.write(b"#03,04,00\r")
        time.sleep(0.5)
        port.flush_input()
        link = Link(port, log)
        return port, Amp(link, image, log)
    except Exception:
        port.close()
        raise


def cmd_probe(args, log):
    image = load_image(args)
    port, amp = connect(args, log, image, select_source=False)
    try:
        amp.get_versions(attempts=2)
        amp.print_versions()
        if amp.up_to_date():
            say(log, "This amplifier already runs the versions in the update file - nothing to do.")
        else:
            say(log, "This amplifier can be updated with 'update'.")
    finally:
        port.close()
    return 0


def cmd_update(args, log):
    image = load_image(args)
    describe_image(image)
    print()
    port, amp = connect(args, log, image, select_source=True)
    stage2 = False
    try:
        # auInitialGetVersions
        amp.get_versions(attempts=2)
        amp.print_versions()
        if amp.up_to_date() and not args.force:
            say(log, "\nThis amplifier already runs the versions in the update file - nothing to do.")
            return 0

        if not args.yes:
            print("\nAbout to update this amplifier. It takes roughly 5-10 minutes at 9600 baud.\n"
                  "Do not switch the amplifier off or unplug the cable once stage 2 has started.")
            try:
                answer = input("Type 'yes' to continue: ")
            except EOFError:
                answer = ""
            if answer.strip().lower() != "yes":
                say(log, "Cancelled - nothing was changed.")
                return 1

        # auCheckExistingFlash / auDownloadFile
        pages = image.pages_to_stage(amp.flash_size)
        say(log, "\nStage 1/2: copying the firmware into the amp's flash store (%d pages of 4 KB)." % len(pages))
        todo = amp.pages_needing_write(pages)
        if todo:
            say(log, "  %d of %d pages need writing." % (len(todo), len(pages)))
            amp.write_pages(todo)
            amp.verify_store()
            # auCheckDownloadVersions
            amp.get_versions()
        else:
            say(log, "  The store already holds this firmware (every page CRC matches).")
        plan = amp.plan()
        what = [n for k, n in (("mcu", "main MCU"), ("fpc", "front panel MCU"),
                               ("bt", "Bluetooth"), ("usb", "USB audio")) if plan[k]]

        # auUpgradeFromFlash
        if plan["bt"] or plan["fpc"] or plan["mcu"]:
            say(log, "\nStage 2/2: the amp now reprograms itself from the store (%s)." % ", ".join(what))
            say(log, "  DO NOT SWITCH THE AMPLIFIER OFF. Front panel: A1, PROTECTION, MUTE, D2 lit; SPEAKER A/B flashing.")
            stage2 = True
            amp.start_update(plan)
        elif plan["usb"]:
            say(log, "\nStage 2/2: restarting the amp so it picks up the stored image (%s)." % ", ".join(what))
            stage2 = True
            amp.reboot()
        else:
            say(log, "\nUnit already programmed - the running versions equal the stored ones.")
            amp.print_versions()
            return 0

        finished = amp.watch_update(plan)
        if not finished:
            raise UpdateError(
                "The amplifier did not report completion within %d minutes.\n"
                "Leave it powered until its front panel is back to normal, then run 'probe'\n"
                "to see which version it reports." % round(T_UPDATE / 60))
        say(log, "\nUnit reprogrammed.")

        # auFinalGetVersions - give the unit a moment to come back up, then read back.
        time.sleep(3.0)
        port.flush_input()
        amp.link.reset_parser()
        try:
            amp.get_versions(attempts=4)
            amp.print_versions()
            if amp.up_to_date():
                say(log, "Update complete: the amplifier reports the new versions.")
            else:
                say(log, "The amplifier restarted but does not report the new versions. "
                         "Run 'probe' again in a minute; if it still differs, run 'update' again.")
                return 2
        except UpdateError:
            say(log, "The amp restarted but did not answer the version query yet. Run 'probe' to check,\n"
                     "or in standby press Speaker A/B then A2: a blue A2 LED means MCU v4.00 is installed.")
        return 0
    except KeyboardInterrupt:
        progress_done()
        if stage2:
            say(log, "\nInterrupted. The amp carries on reprogramming by itself - DO NOT switch it off\n"
                     "until its front panel is back to normal. Then run 'probe' to check the result.")
        else:
            say(log, "\nInterrupted during stage 1. The amp's running firmware was not touched;\n"
                     "run 'update' again whenever you like (pages already written are skipped).")
        return 130
    except UpdateError as e:
        progress_done()
        if stage2:
            raise UpdateError(str(e) + "\nThe amp may still be reprogramming itself - do not switch it off "
                              "until its front panel is back to normal.")
        raise UpdateError(str(e) + "\nThis happened before stage 2, so the firmware the amp runs has not been "
                          "touched.\nIt is safe to run 'update' again; pages already written are skipped.")
    finally:
        port.close()


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Cambridge Audio CXA61 firmware updater for macOS/Linux. Reads the firmware out of the vendor updater .exe.")
    p.add_argument("command", choices=["info", "ports", "probe", "update", "extract"],
                   help="info: show what is in the updater/firmware file; ports: list serial ports; "
                        "probe: read the amp's versions; update: perform the update; "
                        "extract: save the image embedded in an updater .exe as a .hex file")
    p.add_argument("file", nargs="?",
                   help="the vendor's updater .exe, or a firmware .hex "
                        "(default: look next to this script)")
    p.add_argument("--port", help="serial device, e.g. /dev/cu.usbserial-A50285BI (default: auto-detect)")
    p.add_argument("--baud", type=int, default=9600, choices=[9600, 115200], help="default 9600")
    p.add_argument("--firmware", help="same as giving the file as an argument")
    p.add_argument("--output", help="for extract: name of the .hex file to write")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation before updating")
    p.add_argument("--force", action="store_true",
                   help="run the update sequence even if the amp already reports the new versions")
    p.add_argument("--allow-other-firmware", action="store_true",
                   help="use an image this script has not been checked against "
                        "(for example from a newer updater)")
    p.add_argument("--log", help="write a transcript of the serial traffic to this file "
                                 "(default for probe/update: cxa_update_<time>.log)")
    p.add_argument("--no-log", action="store_true", help="do not write a transcript")
    p.add_argument("--verbose", "-v", action="store_true", help="print every frame sent and received")
    args = p.parse_args(argv)
    if args.file:
        if args.firmware:
            p.error("give the file either as an argument or with --firmware, not both")
        args.firmware = args.file

    log_path = None
    if args.command in ("probe", "update") and not args.no_log:
        log_path = args.log or time.strftime("cxa_update_%Y%m%d_%H%M%S.log")
    log = Log(log_path, args.verbose)
    log.note(TOOL)
    try:
        handler = {"info": cmd_info, "ports": cmd_ports, "probe": cmd_probe, "update": cmd_update,
                   "extract": cmd_extract}
        return handler[args.command](args, log)
    except UpdateError as e:
        progress_done()
        print("\nERROR: %s" % e, file=sys.stderr)
        log.note("ERROR: %s" % e)
        return 1
    except KeyboardInterrupt:
        progress_done()
        print("\nInterrupted.")
        return 130
    finally:
        log.close()
        if log_path:
            print("(Transcript: %s)" % log_path)


if __name__ == "__main__":
    sys.exit(main())
