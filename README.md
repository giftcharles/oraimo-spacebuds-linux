# oraimo SpaceBuds on Linux

Unofficial Ubuntu CLI + protocol notes for oraimo SpaceBuds (JieLi
chipset), reverse-engineered from oraimo-sound 1.3.2 for interoperability.
No vendor binaries are shipped here — bring your own `adb pull`.

## What works today (no pairing tricks)

- A2DP/HFP audio via PipeWire + BlueZ, battery via BlueZ.
- `spacebuds battery` — battery readout.
- `spacebuds watch [secs]` — passive SPP monitor: per-bud battery,
  buds' live (rotating) LE address, link-info decode. No pairing needed.

## Gated on an LE bond

`status` (live noise mode), `anc [off|anc|transparency]`,
`eq` (preset dump), `pair` (verify + cache) speak JL RCSP over BLE
GATT (`…ae00` write / `…ae01` notify). They are implemented against the
map in `PORT.md` and go live wherever the buds grant an LE bond —
our test pair refuses non-phone hosts, see `HANDOVER.md` §5.

## Protocol (short)

- Transport: JL RCSP, `FE DC BA|flags|op|len:2BE|sn|params|EF`;
  GetSysInfo=7 / SetSysInfo=8, function `0xFF`.
- Attr 13 = noise mode (0 off / 1 ANC / 2 transparency, 9-byte
  VoiceMode); attrs 4/12 = EQ; get-voice mask 8192, get-EQ mask 4112.
- SPP ch1 = HFP (silent otherwise); ch10 = link-info push only
  (`[03][type][len:2BE][data]`; `03` = per-bud battery out-of-case).
- Full map: `PORT.md`. Day log + failures: `HANDOVER.md`.

## Use

Requires `bleak` (`pip install --user bleak`), BlueZ 5.85+.

```
alias spacebuds="python3 /path/to/spacebuds.py --no-intro"
spacebuds battery
spacebuds watch 60
spacebuds status
spacebuds anc transparency
```

Status: works-as-documented on Ubuntu 24.04+; ANC/EQ switching needs
the LE bond described above.
