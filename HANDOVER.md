# Handover: oraimo SpaceBuds → Ubuntu port

Date: 2026-11-09. Goal: port the oraimo-sound Android app's earbud
features (ANC switching, EQ, touch-key config) to this Ubuntu PC.
Status: **read path shipped, SET path blocked on an LE bond the buds
refuse this machine.** Everything below reproduces from this folder.

## 1. Environment

- PC: Ubuntu, Intel BT (CNVi, driver `btintel_pcie`), BlueZ 5.85,
  PipeWire 1.6.2 + WirePlumber. Adapter re-enumerated hci0→hci1→hci0
  during the day; one firmware wedge required
  `modprobe -r/-reload btintel_pcie` (NOT btusb) + service restarts.
- Phone (assistant only): Pixel 9a, `53101JEBF11300`, Android 17,
  oraimo-sound 1.3.2 present and running. Stock build — no root, so no
  HCI snoop (`/data/misc/bluetooth/logs` unreadable).
- Buds: oraimo SpaceBuds, classic `82:08:20:02:69:97`, LE identity
  rotates (`98:4C…` → `1C:8E…` → `2B:4E…` → `A2:51…`, same tail
  `20:02:69:97`). Also present: oraimo FreePods lite (paired, unused).
- Builder container `mobile-builder` (10.10.20.90) has apktool 2.7.0
  but its Tailscale link (~40 KB/s) can't take a 72 MB upload, so all
  decomp was done locally (`/tmp/opencode/tools`: apktool.jar,
  jadx-1.5.6; decode `/tmp/opencode/oraimo/apk-decoded`,
  sources `/tmp/opencode/oraimo/jadx-out`).

## 2. Artifacts (this folder)

- `oraimo-sound-1.3.2-base.apk` (72M) + `split_config.arm64_v8a` (44M)
  + `split_config.xxhdpi` (74K), pulled via `adb pull` — see
  `project.json` for device paths, sizes, sha256.
- `spacebuds.py` — the CLI (stdlib + `bleak`, animated ASCII intro,
  `--no-intro`). Aliased as `spacebuds` in `~/.zshrc`/`~/.bashrc`.
  Symlinked at session root.
- `PORT.md` — protocol spec + dated evidence log (§8) + verdict (§9).
- `project.json`, `HANDOVER.md` (this file).

## 3. Findings that held up

- Chipset is **JieLi** (`sdptool browse`: `JL_A2DP`/`JL_HFP`/`JL_SPP`;
  `libjl_bluetooth.so` etc. in the arm64 split). The APK also bundles
  BES + Bluetrum SDKs for other models — ignore them for SpaceBuds.
- Control protocol is **JL RCSP**: framing
  `FE DC BA|flags|op|len:2BE|sn|[xmop]|params|EF`
  (`ParseHelper.packSendBasePacket` / `RcspPacketParse`), opcodes
  GetSysInfo=7 / SetSysInfo=8, function byte `0xFF`.
- Command map (`com/jieli/bluetooth/`, `utils/CommandBuilder`):
  attr 13 = noise mode (`VoiceMode`: 0 off / 1 ANC / 2 transparency),
  attrs 4/12 = EQ data/preset, get-voice mask 8192, get-EQ mask 4112,
  get = `GetSysInfo(FF,mask)`, set = `SetSysInfo(FF,[13:9B])`
  (9-byte VoiceMode echoed back with byte0 replaced).
- SPP ch1 = HFP (silent to anything else). SPP ch10 = link-info push
  only: `[03][type][len:2BE][data]` records (`01:a2c199`,
  `02:<current LE bdaddr>`, `03:[battA][battB][ff]` out-of-case).
  RCSP bytes sent on ch1/ch10 are ignored. No SPP SET path exists.
- `03` record is **per-bud battery**, proven 3× (record byte ==
  BlueZ %: 70/70, 60/60, 50/50; monotonic decline; in-case uses a
  different encoding, e.g. `bbc657`). The early "ANC-byte" reading was
  a coincidence — do not resurrect it.
- BlueZ already gives battery + A2DP/HFP/AVRCP; touch gestures on the
  buds cycle ANC with no software involved.

## 4. What works today (verified live)

- `spacebuds battery` — BlueZ battery %. `spacebuds watch [secs]` —
  SPP ch10 decode incl. live LE address. A2DP audio as default sink
  (one stale half-link needed a classic reconnect).
- `spacebuds status|anc|eq` — implemented against the §3 map, **cannot
  run**: they need the LE GATT link (`…ae00` write / `…ae01` notify).

## 5. The blocker (all failures)

LE GATT (`0000ae00-…`) is unreachable from this PC, six ways:
1. `bluetoothctl pair <LE>` → `AuthenticationFailed` (also with an
   agent session; buds only accept pairing in a short lid-open window
   and reject outside it).
2. Our first pair script fired pair+trust+quit instantly, aborting the
   exchange — fixed with paced Popen; the real exchange then showed
   connect-then-reject, so this wasn't the root cause.
3. Post-pair-verify → `br-connection-key-missing` (no LTK ever stored).
4. BlueZ GATT connect when visible+connected → `NotSupported`
   (BlueZ treats the dual-mode object as BR/EDR).
5. GATT-over-BR/EDR on classic `82:08` → 0 services (twice, pre/post
   adapter reset). Control is LE-only by firmware design.
6. Advertisements vanish in seconds; a stale `82:05` LE bond was
   removed (it pointed nowhere). Fresh classic re-bond via GNOME did
   not derive LE keys (no CTKD from these buds).
- Dead ends deliberately skipped: phone HCI snoop (stock Pixel, needs
  root), 72 MB re-upload to mobile-builder (link too slow), ADB UI
  automation bridge (fragile, parked).

## 6. UX bugs fixed in the CLI (don't regress)

- Bare `TimeoutError` from bleak (empty message) → wrapped with
  context in `run_session`; RCSP wait wraps into "buds silent…".
- `pair` no longer pairs — GNOME owns all pairing/connection; `pair`
  only verifies ae00 + caches to `~/.cache/spacebuds.json`.
- SPP `watch` got a one-retry connect (buds reset first attempts).

## 7. To reopen

1. Lid open + hold-free wait for white flash + GNOME Connect on the
   **LE/second** SpaceBuds entry, then `spacebuds pair`. If ae00
   verifies, `anc`/`eq`/`status` work unmodified.
2. Otherwise: build the Pixel-ADB bridge (buds→phone, scripted
   `uiautomator`/`input tap` ANC toggles) — needs phone BT on and buds
   moved off Ubuntu; flaky by nature.
3. Otherwise: declare Ubuntu = audio + battery + `watch`; ANC via
   touch/phone. Nothing further to probe — SPP/RCSP/BLE paths are all
   exhausted and logged above.
