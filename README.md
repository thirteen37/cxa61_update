# cxa61_update

Update the firmware of a Cambridge Audio **CXA61** or **CXA81** amplifier from
macOS or Linux. Cambridge Audio's own updater only runs on Windows.

This is a Python port of the update procedure in Cambridge Audio's
"CXAx1 Updater to MCU v4.0" (AP364281, which identifies itself as
*CXAx1 Upgrader v2.0*). You supply that vendor updater; the script reads the
firmware out of it and talks to the amp over its RS232 port.

> [!WARNING]
> **This has not been run against a real amplifier yet.** It has only been
> verified against a simulated one (details under
> [Verification](#verification)). Flashing firmware can leave a device
> unusable. This is an unofficial tool with no warranty: if you use it, you do
> so at your own risk. If you have access to a Windows PC or VM, the vendor
> updater is the safer choice.

Unofficial. Not affiliated with, endorsed by, or supported by Cambridge Audio.

## Status

| | |
|---|---|
| Sends the same bytes as the vendor updater (simulated amp) | Verified |
| Linux | Tested, with a pseudo-terminal in place of a serial cable |
| macOS | Not yet run |
| Real USB-to-RS232 cable | Not yet tried |
| Real CXA61 / CXA81 | Not yet tried |

If you try it on real hardware, please open an issue with the result, whether
it worked or not. See [Reporting results](#reporting-results).

## Quick start

```
git clone https://github.com/thirteen37/cxa61_update.git
cd cxa61_update

python3 cxa_update.py info   "AP364281 Cambridge CXAx1 Updater to MCU v4.0.exe"
python3 cxa_update.py probe  "AP364281 Cambridge CXAx1 Updater to MCU v4.0.exe"
python3 cxa_update.py update "AP364281 Cambridge CXAx1 Updater to MCU v4.0.exe"
```

`info` works offline. `probe` only reads from the amp (apart from switching it
on). `update` asks for confirmation before changing anything.

## Requirements

- **Python 3.8 or newer.** Standard library only; there is nothing to install.
- **The vendor updater `.exe`.** See [Getting the firmware](#getting-the-firmware).
- **A USB-to-RS232 cable** in the amp's RS232 socket. Cambridge's instructions
  point to FTDI drivers, so an FTDI-based cable is the safe choice. Whether the
  CXA needs a straight-through or a null-modem cable is not confirmed; if
  `probe` gets no answer, try a null-modem adapter.
- **Linux:** permission to use the serial device, usually membership of the
  `dialout` group (`uucp` on Arch).

## Getting the firmware

This repository contains no Cambridge Audio software or firmware, and none
should be added to it. Ask Cambridge Audio support for the CXA61/CXA81 updater
and pass the `.exe` you receive to the script. Nothing in the `.exe` is
executed; the script only reads the firmware image stored inside it.

The script recognises the image from "Updater to MCU v4.0" by its SHA-256 and
refuses anything else unless told otherwise
(see [Other updater versions](#other-updater-versions)).

## Commands

```
python3 cxa_update.py info    FILE   # what is in this updater? (offline)
python3 cxa_update.py ports          # list candidate serial ports
python3 cxa_update.py probe   FILE   # find the amp and show its versions
python3 cxa_update.py update  FILE   # perform the update
python3 cxa_update.py extract FILE   # save the embedded image as a .hex file
```

`FILE` is the vendor updater `.exe`, or a `.hex` saved earlier with `extract`.

| Option | Effect |
|---|---|
| `--port DEVICE` | Use this serial device instead of auto-detecting (`/dev/cu.usbserial-*` on macOS, `/dev/ttyUSB*` on Linux) |
| `-v`, `--verbose` | Print every frame sent and received |
| `--yes` | Skip the confirmation prompt |
| `--force` | Run the update even if the amp already reports the new versions |
| `--log FILE`, `--no-log` | Where to write the serial transcript, or don't write one |
| `--allow-other-firmware` | Accept an image the script has not been checked against |

`probe` and `update` write a transcript of the serial traffic to
`cxa_update_<time>.log` by default. Attach it to any bug report.

## What the update does

The sequence is the vendor updater's.

1. **Find the amp.** It must identify itself as a CXA61 (`CA0837`) or CXA81
   (`CA0877`). It is switched on if it is in standby, and source A1 is selected.
2. **Read versions:** what the amp is running, and what is staged in its
   internal flash store.
3. **Stage 1 - copy the image into the flash store**, one 4 KB page at a time,
   skipping pages whose CRC already matches. The amp then computes the CRC of
   each stored image and the script compares it with the file. The firmware the
   amp runs from is not touched in this stage.
4. **Stage 2 - the amp reprograms itself from the store** and reboots. The
   script sends the trigger and then only watches.

Start with the amp in standby: that is the path the vendor updater takes.
Expect roughly 5-10 minutes at 9600 baud.

## If something goes wrong

- **During stage 1** (cable pulled, timeout, Ctrl-C, CRC mismatch): the running
  firmware has not been touched. Run `update` again; pages already written are
  skipped.
- **During stage 2:** do not switch the amp off. It is reprogramming itself and
  no longer depends on the computer. Wait until the front panel is back to
  normal, then run `probe` to see which versions it reports.
- **`probe` finds nothing:** check that the amp has mains power, the cable is
  in the RS232 socket, and try a null-modem adapter.

## Other updater versions

The script knows the procedure of *CXAx1 Upgrader v2.0*. Given a different
updater `.exe`, it will:

- read the image and refuse it if a header checksum or CRC record does not match;
- report whether the updater program still identifies itself as v2.0;
- stop, and continue only if the command is repeated with `--allow-other-firmware`.

It can vouch for the image, not for the procedure: a different updater may also
update the amp differently. If the program version is not v2.0, do not use the
flag without first checking that the procedure is unchanged.

## Trying it without an amplifier

`fake_cxa.py` is a pretend CXA61 on a pseudo-terminal:

```
python3 fake_cxa.py                                # prints a device path
python3 cxa_update.py update FILE --port <that path>
```

It can start in standby, report other versions, stop answering part-way, or
store a page wrongly, to exercise the error handling (`--help` lists the
options). Its behaviour is inferred from the vendor updater; it is not real
amplifier firmware, and passing against it proves nothing about a real amp.

## Verification

- The unmodified vendor updater was run under Mono against `fake_cxa.py`, and
  every byte it sent was captured.
- `cxa_update.py` was run against the same simulated amp. Its output begins
  with the complete vendor byte stream (255,144 bytes for a full update from
  standby). The only addition is a final version read-back.
- The same held for an amp already on the new version, a flash store that
  already held the image, and block sizes of 64 and 100 bytes.
- Failure cases were exercised: an amp that stops answering, a wrongly stored
  page (the update stops before stage 2), no completion report, and damaged
  images.

What this does not show: that a real amplifier answers the way the simulated
one does. The amplifier's side of the protocol is undocumented.

## Protocol notes

9600 baud, 8N1, no flow control. Two protocols share the port:

- ASCII control: `#gg,nn[,data]\r`
- Binary service: `0x21`, group, command, length, data

| Sent | Answer | Meaning |
|---|---|---|
| `!AA 8B 00` | `!AA 0B 03 <id>` | flash chip id |
| `!AA 8A 04 a2 a1 a0 02` | `!AA 0A 05 a2 a1 a0 hi lo` | CRC-16 of a 4 KB page (`0x80pppp`) or of stored image *i* (`0x4000i0`) |
| `!AA 0A 00` | `!AA 8A 00` | reset flash session |
| `!AA 0A 07 F5 addr32 20 crc8` | `!AA 8A 00` | erase 4 KB sector |
| `!AA 0A 07 F5 addr32 02 crc8` | `!AA 8A 03 seq status maxblock` | start programming a page |
| `!AA 0A n+2 seq data[n] crc8` | `!AA 8A 01 nextseq` | program data |
| `!0F 91 00` / `!0F 92 00` | `!0F 11 ...` / `!0F 12 ...` | running / stored versions |
| `!0F 89 00` | `!0F 09 07 ...` | status, bootloader versions |
| `!0F 50 01 mask` | `!0F Dx 01 percent` | update from store (`01` main MCU, `02` front panel MCU, `20` Bluetooth) |
| `!0F AA 00` | | reboot |

This table is what the vendor updater sends and expects; the names are
descriptive guesses.

## Reporting results

Reports from real hardware are the most useful contribution. Please open an
issue and include:

- model (CXA61 or CXA81) and the versions `probe` showed before and after;
- operating system and the cable or chipset used, and whether it needed a
  null-modem adapter;
- the `cxa_update_<time>.log` transcript.

Please do not attach or link the vendor updater or firmware.

## Disclaimer

This software is provided as is, without warranty of any kind. The authors are
not responsible for damage to your equipment. Cambridge Audio, CXA61 and CXA81
are trademarks of their respective owner, used here only to say what this tool
works with.

## License

[MIT](LICENSE). The licence covers the code in this repository only, not
Cambridge Audio's updater or firmware, which are not included.
