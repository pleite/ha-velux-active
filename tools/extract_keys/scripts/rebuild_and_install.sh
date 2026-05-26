#!/bin/bash
exec >/tmp/velux-decomp/rb3.log 2>&1
set -ex
cd /tmp/velux-decomp
docker run --rm -u $(id -u hermes-agent):$(id -g hermes-agent) -v /tmp/velux-decomp:/work -w /work eclipse-temurin:17-jre java -jar apktool.jar b --use-aapt1 decoded -o velux-patched-unsigned.apk 2>&1 | tail -10
docker run --rm -u $(id -u hermes-agent):$(id -g hermes-agent) -e LD_LIBRARY_PATH=/work/build-tools/lib64 -v /tmp/velux-decomp:/work -w /work eclipse-temurin:17-jdk bash -c "/work/build-tools/zipalign -p -f 4 velux-patched-unsigned.apk velux-patched-aligned.apk && /work/build-tools/apksigner sign --ks debug.keystore --ks-pass pass:android --key-pass pass:android --out velux-patched.apk velux-patched-aligned.apk && /work/build-tools/apksigner verify velux-patched.apk && echo SIGN_OK"
docker exec adb-server adb uninstall com.velux.active || true
docker cp velux-patched.apk adb-server:/tmp/velux-patched.apk
docker exec adb-server adb install -r /tmp/velux-patched.apk
docker exec adb-server adb shell pm enable com.velux.active
echo COMPLETE > /tmp/velux-decomp/rb3.done
