#!/bin/bash
exec >/tmp/velux-decomp/splits_keep.log 2>&1
set -ex
cd /tmp/velux-decomp
docker run --rm -u $(id -u hermes-agent):$(id -g hermes-agent) -e LD_LIBRARY_PATH=/work/build-tools/lib64 -v /tmp/velux-decomp:/work -w /work eclipse-temurin:17-jdk bash -c "
set -ex
for apk in config.en.apk config.hdpi.apk config.arm64_v8a.apk; do
  /work/build-tools/zipalign -p -f 4 \$apk \${apk}.aligned
  /work/build-tools/apksigner sign --ks debug.keystore --ks-pass pass:android --key-pass pass:android --out \${apk}.signed \${apk}.aligned
done
echo SIGN_DONE
"
docker cp velux-patched.apk         adb-server:/tmp/base.apk
docker cp config.en.apk.signed      adb-server:/tmp/config.en.apk
docker cp config.hdpi.apk.signed    adb-server:/tmp/config.hdpi.apk
docker cp config.arm64_v8a.apk.signed adb-server:/tmp/config.arm64_v8a.apk
docker exec adb-server adb install-multiple -r /tmp/base.apk /tmp/config.en.apk /tmp/config.hdpi.apk /tmp/config.arm64_v8a.apk
docker exec adb-server adb shell pm enable com.velux.active
echo COMPLETE > /tmp/velux-decomp/splits_keep.done
