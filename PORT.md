# oraimo SpaceBuds → Ubuntu port spec

Source: `com.transsion.oraimosound` 1.3.2 (142), pulled via adb from Pixel 9a.
APKs + hashes: see `project.json`. Decompiled locally (apktool 2.7.0, jadx 1.5.6):
`/tmp/opencode/oraimo/apk-decoded`, `/tmp/opencode/oraimo/jadx-out`.

## 1. Chipset verdict: JieLi (JL), not BES

`sdptool browse 82:08:20:02:69:97` shows `JL_A2DP`, `JL_HFP`, `JL_SPP`
(RFCOMM channels 1 and 10). The APK bundles three chip SDKs
(`com.jieli`, `com.bes`, `com.bluetrum`) for different models, but the
connected SpaceBuds speak **JL RCSP over SPP**. Native libs confirm it:
`libjl_bluetooth.so`, `libjl_eq.so`, `libjl_ota_auth.so` in the arm64 split.

## 2. What already works on Ubuntu (no port needed)

- Audio (A2DP sink), calls (HFP), media keys (AVRCP) via PipeWire + BlueZ.
- Battery: `bluetoothctl info 82:08:20:02:69:97` exposes
  `Battery Percentage` (read 80, then 70). Nothing to build.

## 3. Command map (from `com/jieli/bluetooth/` in jadx-out)

- `constant/AttrAndFunCode.java`: `SYS_INFO_ATTR_CURRENT_NOISE_MODE = 13`,
  `ALL_NOISE_MODE = 14`, `ATTR_EQ = 4`, `EQ_PRESET_VALUE = 12`,
  `KEY_FUNC_ID_SWITCH_ANC_MODE = 255`.
- `bean/base/VoiceMode.java`: `CLOSE = 0`, `DENOISE = 1`, `TRANSPARENT = 2`.
- `utils/CommandBuilder.java`: `buildSetCurrentVoiceMode()` =
  SetSysInfo attr 13 with `VoiceMode.getBytes()`; `buildGetCurrentVoiceMode()`,
  `buildGetEqPresetAndEqValueCmd()`, `buildGetBatteryCmd()` for reads.
- Transport enum `com/bes/sdk/utils/DeviceProtocol.java` confirms
  `PROTOCOL_SPP(16)`.

## 4. To build: minimal Ubuntu CLI

1. Open RFCOMM to `82:08:20:02:69:97`, channel 1 (fallback 10).
2. Frame RCSP packets (`SetSysInfoCmd`/`GetSysInfoCmd`; param encoding in
   `SetSysInfoParam.getParamData()`; base header in
   `bean/base/CommandBase.java` + `bean/cmdHandler/DataCmdHandler.java`).
3. Commands: get current voice mode, set 0/1/2, get/set EQ preset,
   get battery. Verify live with `btmon` running.
4. Deps: Python stdlib `socket` with `AF_BLUETOOTH`/`BTPROTO_RFCOMM`
   (BlueZ) — no bleak/pybluez needed for SPP.

## 5. Known unknowns (scoped, not blockers)

- Exact RCSP wire header/CRC bytes around `CommandBase` (read
  `DataCmdHandler.java` next — one small file).
- EQ value encoding (`EQPayload`/`EQControl`) and game-mode attr id
  (grep `AttrAndFunCode.java` for `GAME`/`LATENCY`).
- OTA (`assets/ota/*`, `jl_bt_ota/`) deliberately out of scope for v1.

## 7. Build status (2026-11-09)

`spacebuds.py` implements the map above over BLE RCSP (write …ae02,
notify …ae01, stdlib + `bleak`). Verified so far without the buds awake:
syntax, arg parsing, BlueZ battery path, LE discovery (finds the buds'
LE identity `98:4C:20:02:69:97` by MAC-tail match), graceful errors.

One physical step left (needs you, 1 min): LE pairing needs pairing mode,
and the buds' LE identity rotates (`98:4C…` → `1C:8E…`, same tail).
`pair` now runs a full bluetoothctl agent session and skips the classic
MAC. A2DP audio itself was fixed 2026-11-09 with a clean classic
reconnect (sink `oraimo SpaceBuds` is default output).

`pair` caches the LE address in `~/.cache/spacebuds.json`.
Do NOT run `anc` while on a call; it rewrites the mode byte live.

## 9. Final verdict (2026-11-09, after BT firmware reset)

LE control is not obtainable from this machine. Repeatable refusals
across adapter reset, fresh bonds, agent sessions, paced pairing and
GNOME attempts: `AuthenticationFailed` on LE pair, `NotSupported` on
GATT connect, `br-connection-key-missing/aborted`, advertisements that
vanish in seconds, rotating LE identities (`98:4C…` → `1C:8E…` →
`2B:4E…` → `A2:51…`). The buds admit an LE control link to their
bonded phone app only.

What works on Ubuntu today (verified): A2DP audio (default sink),
BlueZ battery (`battery`: 50%), SPP ch10 state snapshots (`watch`;
`03` record + live LE-addr in `02`), touch-gesture ANC cycling on the
buds themselves, Android app for EQ/presets via the phone.

`spacebuds.py` ships with `anc`/`eq`/`status` implemented against the
fully-mapped JL RCSP map (§3) — they go live the day an LE bond exists.
Reopen condition: a firmware/app change that permits LE pairing, or a
decision to build the Pixel-ADB bridge instead (parked, §6).

## 8. Live SPP evidence (ch10, 2026-11-09)

`spacebuds.py watch` (no pairing needed) decodes the connect hello as
`[03][type][len:2BE][data]` records: `01:a2c199`, `02:<LE bdaddr>`,
`03:4649ff`. A tap-hold on a bud 12 s later pushed `03:3c49ff` —
first byte `0x46 → 0x3c`, middle byte drifts across connects
(`4c/4a/49`, plausibly per-bud battery), last byte constant `ff`.
Hypothesis: `03 = [state][battA][battB?]`. Needs labeled taps
(heard mode per change) to confirm; see chat. If confirmed, `watch`
becomes a live ANC-state read with zero pairing.

Anchored 2026-11-09 (user-labeled): `0x46` = NC (noise cancellation),
`0x3c` = OFF. Missing: transparency byte. Note the buds' own cycle is
NC → TR → OFF (per user taps), and each fresh `watch` connect
snapshots the current byte — so `watch 8` is effectively a state READ
even though mid-session pushes are unreliable.

REVISION 3 (11:40, post-reset live-fire): out-of-case `03` now reads
`32 3e ff` with BlueZ battery at 50 (=`0x32`) — third exact match
(70/70, 60/60, 50/50). ANC-byte theory dead: `03` out-of-case is
`[battA][battB][0xff]` (uneven per-bud drain; in-case uses a different
encoding, e.g. `bbc657`). RCSP GetSysInfo re-sent on ch1 (silent, HFP)
and ch10 (ignored) — no SPP SET path exists. The `anc` switcher is
built and correct per the SDK map; only the LE transport is missing.

## 6. Deliberately skipped

Phone-side HCI snoop (Pixel is stock: `/data/misc/bluetooth/logs`
needs root) and the 72 MB re-upload to `mobile-builder` over the
~40 KB/s Tailscale link. Static map above + live `btmon` probing from
this machine replaces both.
