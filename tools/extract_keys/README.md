# Velux ACTIVE — sign-key extraction toolkit

This directory contains the smali patches and helper scripts used to
extract `HashSignKey` and `SignKeyId` from the official **Velux
ACTIVE Android app** (NetatmoApp engine) by patching three log
statements into `android/br1.smali` and rebuilding the APK.

This is a reproducible, "do it again next time the app rotates"
toolkit — not a runtime dependency. The HA integration itself only
needs the two captured key values.

> See `docs/EXTRACTING_SIGN_KEY.md` for the user-facing recipe.
> This README is the **maintainer** copy: it documents the exact
> smali offsets, the docker-based build env, and the captured
> sample.

## What gets injected

The Velux app's signature is built inside one method on
`android/br1.smali` (class name obfuscated; the method takes a
`Landroid/e73;` request and a `Landroid/fid;` carrying both key
bytes and key-id bytes). Three `Log.d("velux-debug", …)` calls are
spliced in:

| #   | Where in the smali method            | What it logs                                           |
|-----|--------------------------------------|--------------------------------------------------------|
| 1   | After the StringBuilder that builds the pre-hash | `Hashing string: <item><value><ts><nonce><dev_id>` |
| 2   | After `fid.b()` returns the 32-byte key  | `HashSignKey: <64 hex chars>`                       |
| 3   | After `fid.c()` returns the 16-byte id   | `SignKeyId: <hex>`                                  |

The hex encoding is done with `BigInteger(1, bytes).toString(16)` so
that no library helpers are pulled in (keeps the patch minimal and
self-contained).

The exact smali blocks live in `smali_hooks/`:

- `br1_injection_1_prehash.smali`
- `br1_injection_2_hashsignkey.smali`
- `br1_injection_3_signkeyid.smali`

Splice each block at the marked positions in `br1.smali` (the
recipe in `docs/EXTRACTING_SIGN_KEY.md` walks through the anchor
lines). Renumber `v13/v14/v15` registers if the surrounding method
already uses them.

## What the build env looks like

- `apktool.jar` (≥ 2.10) — disassemble/reassemble
- `eclipse-temurin:17-jdk` docker image — for `zipalign` + `apksigner`
- Android `build-tools` (zipalign, apksigner binaries)
- `debug.keystore` — generate once with
  `keytool -genkey … -keystore debug.keystore -alias androiddebugkey`
- `adb` running inside an `adb-server` docker container connected
  to the target Android device over USB
- The host script `scripts/rebuild_and_install.sh` runs all of the
  above (apktool b → zipalign → apksigner → adb install -r → pm
  enable). It assumes the working directory contains the decoded
  tree, the apktool jar, build tools, and the keystore.

`scripts/sis.sh` and `scripts/sis_keep.sh` are split-APK variants
(needed when the app is shipped as a base APK + per-arch / per-DPI /
per-locale splits, as Velux's distribution does in Play Store
deliveries).

## Captured sample

For traceability, one validated capture from this user's account on
2026-05-26 produced:

- HashSignKey: 32 bytes (hex), unique per pairing
- SignKeyId:   16 bytes (hex), unique per pairing
- pre-hash format observed:
  `target_position` + `14` + `1779484789` + `0` + `5636133219200932`

The values themselves are **not committed** — they are user secrets
and must be re-extracted by anyone running this toolkit for their
own account.

## When to re-run

The key does not rotate on its own. You only need to re-extract if:

- Pedro unpairs and re-pairs the bridge in the Velux ACTIVE app
  (this generates a fresh HashSignKey + SignKeyId).
- The Velux app's smali layout changes enough that the patch
  anchors drift — in which case the three injections need to be
  re-located in the new version of `br1.smali`. The anchor strings
  to grep for are:
  - `fid.c()` call site for injection 3 (SignKeyId)
  - the `Ljava/lang/String;->getBytes` that feeds the HMAC for
    injection 1 (pre-hash)
  - the `fid.b()` call site for injection 2 (HashSignKey)
