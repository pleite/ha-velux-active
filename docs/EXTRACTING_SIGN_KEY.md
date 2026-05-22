# Extracting `HashSignKey` and `SignKeyId` from the VELUX ACTIVE app

> **Why you need this**: see `docs/SIGNING.md`. Without these two
> values, your Home Assistant cannot open VELUX windows — only close
> them or move shutters. The values are per-user secrets; do not share
> them.

You only need to do this once. The key does not rotate.

There are three known extraction paths, ranked by effort.

---

## Path 1 — Android APK patching (recommended, no Mac required)

### What you need

* Any Android device or emulator (BlueStacks/Genymotion work)
* `adb` from the Android platform-tools
* `apktool` (`sudo apt install apktool` or
  https://ibotpeaches.github.io/Apktool/)
* `keytool` and `apksigner` (ship with the Android SDK)
* VELUX ACTIVE APK — version `1.10.0.0` recommended; download from
  APKMirror or APKPure

### Steps

1. **Decompile the APK**

   ```bash
   apktool d "VELUX ACTIVE_1.10.0.0.apk" -o velux_decoded
   ```

2. **Apply the ZTHawk smali patches**

   Clone https://github.com/ZTHawk/velux_active_patches, then copy the
   patched files from the `VELUX ACTIVE with NETATMO_1.10.0.0/`
   directory over the matching paths inside `velux_decoded/`. The
   patches add `Log.i("velux-debug", ...)` calls at the points where
   the app builds the signature.

3. **Repack and sign**

   ```bash
   apktool b velux_decoded -o velux_patched.apk
   keytool -genkey -v -keystore debug.keystore -alias androiddebugkey \
           -keyalg RSA -keysize 2048 -validity 10000 \
           -storepass android -keypass android \
           -dname "CN=Android Debug,O=Android,C=US"
   apksigner sign --ks debug.keystore --ks-pass pass:android \
                  --key-pass pass:android velux_patched.apk
   ```

4. **Install and login**

   ```bash
   adb install -r velux_patched.apk
   adb logcat -s velux-debug
   ```

   Launch the app on the device, log in with your VELUX credentials,
   then move a window from any position to any other position.

5. **Capture the keys from logcat**

   You should see lines like:

   ```
   I/velux-debug: HashSignKey=DEADBEEF...  (64 hex chars)
   I/velux-debug: SignKeyId=69cfc57175bf211466038503  (24 hex chars)
   ```

6. **Configure Home Assistant**

   Settings → Devices & Services → VELUX ACTIVE → **Configure**.
   Paste `HashSignKey` into the *Hash sign key* field and `SignKeyId`
   into the *Sign key ID* field. Click *Submit*.

7. **Test**

   `Developer Tools → Services → cover.set_cover_position`,
   pick your window, set position to 50, fire.

   The window should move within a few seconds. Check the integration
   logs — you should see `set_cover_position(... signed=True)` and a
   clean response.

---

## Path 2 — iOS keychain dump (Mac required)

1. Connect your iPhone to a Mac with Xcode installed.
2. Trust the computer on the iPhone.
3. Install `idb` from Facebook
   (https://github.com/facebook/idb): `brew tap facebook/fb && brew install idb-companion`.
4. List the VELUX app's keychain entries:
   ```bash
   idb shell -u <udid> com.velux.active "security dump-keychain"
   ```
5. Look for entries whose `acct` field contains `sign`, `hash`,
   `netatmo`, or `velux`. The `data` field holds the value — base64
   for `HashSignKey`, short hex for `SignKeyId`.

This path bypasses APK patching entirely but requires an iOS dev
provisioning profile in some iOS versions.

---

## Path 3 — Live MITM during pairing

This only works if you have **not** yet paired your bridge. In short:

1. Install a TLS MITM proxy (HTTP Toolkit recommended) on your iPhone.
2. Force-quit the VELUX app, then re-pair the bridge from inside the
   app while capture is running.
3. Watch for a websocket frame on `wss://app-ws.velux-active.com/ws/`
   that contains the literal string `sign_key` — the value beside it
   is `HashSignKey`.

If your bridge is already paired you cannot trigger the key-delivery
push from the cloud without unpairing first (not recommended — it
deletes all your scenarios).

---

## Keeping the keys out of version control

Never commit `HashSignKey` to a public repo. The HA options flow
stores it in `core.config_entries` (encrypted via the storage layer
when HA is configured with secret protection). Treat it like a
password — it grants anyone who has it the ability to open your
skylights remotely.

If you ever suspect the key is compromised, unpair and re-pair your
bridge in the official app — that rotates the key.
