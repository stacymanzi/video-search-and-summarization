#!/usr/bin/env bash
# Synthetic validation in the deployer image, without network or live volumes.
set -euo pipefail
root=$(mktemp -d /tmp/rt-cv-cache-test.XXXXXX)
trap 'rm -rf -- "$root"' EXIT
mkdir -p "$root/bin"
printf '#!/bin/sh\nprintf "NVIDIA H100 NVL\\n"\n' > "$root/bin/nvidia-smi"
chmod +x "$root/bin/nvidia-smi"
export PATH="$root/bin:$PATH"
jq '.version="test-v1"' /deploy/rt-cv-checkpoint.json > "$root/manifest.json"
cache="$root/models/test-v1/cv-cache"
while IFS= read -r file; do
    mkdir -p "$cache/$(dirname "$file")"
    printf abc > "$cache/$file"
done < <(jq -r '.files[].name' "$root/manifest.json")
printf '/opt/storage/siglip_v2_v1.1.onnx\n' > "$cache/siglip_v2_v1.1.onnx_batch16.plan.meta"
while IFS= read -r file; do
    hash=$(sha256sum "$cache/$file" | cut -d ' ' -f 1)
    bytes=$(stat -c %s "$cache/$file")
    jq --arg name "$file" --arg hash "$hash" --argjson bytes "$bytes" \
       '(.files[] | select(.name==$name)) |= (.sha256=$hash | .bytes=$bytes)' \
       "$root/manifest.json" > "$root/new.json"
    mv "$root/new.json" "$root/manifest.json"
done < <(jq -r '.files[].name' "$root/manifest.json")
cp "$root/manifest.json" "$root/good.json"
verify() { bash /deploy/restore-rt-cv-cache.sh "$root/manifest.json" "$root/models"; }
reject() { if verify > "$root/output" 2>&1; then echo "FAIL: $1 accepted"; exit 1; fi; echo "PASS: $1 rejected"; }
asset="$cache/siglip_v2_v1.1.onnx"
engine="$cache/siglip_v2_v1.1.onnx_batch16.plan"
touch -d '2020-01-01' "$engine"
inode=$(stat -c %i "$engine")
[ "$(verify)" = "$cache" ]
[ "$(stat -c %Y "$engine")" = "$(stat -c %Y "$asset")" ]
[ "$(stat -c %i "$engine")" = "$inode" ]
[ "$(stat -c %u:%g "$engine")" = 1000:1000 ]
[ -f "$cache/.siglip_v2_v1.1.done" ]
[ "$(readlink "$cache/siglip2_v1.1_weights.bin")" = siglip_v2_v1.1_weights.bin ]
echo 'PASS: timestamp normalization, no model copy, UID 1000, marker and alias'
verify >/dev/null
echo 'PASS: warm reuse'
printf abd > "$asset"
reject 'same-size corruption'
printf ab > "$asset"
reject 'partial file'
printf abc > "$asset"
mv "$asset" "$root/asset"
reject 'missing file'
ln -s "$root/asset" "$asset"
reject 'file symlink'
rm "$asset"
mv "$root/asset" "$asset"
jq '.files[0].name="../escape"' "$root/good.json" > "$root/manifest.json"
reject 'path traversal'
jq '.files[0]=.files[1]' "$root/good.json" > "$root/manifest.json"
reject 'duplicate artifact'
jq '.compatibility.vision_encoder_batch=16' "$root/good.json" > "$root/manifest.json"
reject 'incorrect encoder batch'
cp "$root/good.json" "$root/manifest.json"
printf '#!/bin/sh\nprintf "NVIDIA H100 80GB HBM3\\n"\n' > "$root/bin/nvidia-smi"
reject 'different GPU variant'
printf '#!/bin/sh\nprintf "NVIDIA H100 NVL\\n"\n' > "$root/bin/nvidia-smi"
rm "$cache/siglip2_v1.1_weights.bin"
ln -s /tmp/unexpected "$cache/siglip2_v1.1_weights.bin"
reject 'wrong weights alias'
rm "$cache/siglip2_v1.1_weights.bin"
verify >/dev/null
echo 'PASS: recovery after validation failures'
