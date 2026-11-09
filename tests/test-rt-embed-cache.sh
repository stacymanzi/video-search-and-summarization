#!/usr/bin/env bash
# Run in the deployer image, network disabled, with no live model volume.
set -euo pipefail
root=$(mktemp -d /tmp/rt-embed-cache-test.XXXXXX)
trap 'rm -rf -- "$root"' EXIT
mkdir -p "$root/bin"
printf '#!/bin/sh\nprintf "NVIDIA H100 NVL\\n"\n' > "$root/bin/nvidia-smi"
chmod +x "$root/bin/nvidia-smi"
export PATH="$root/bin:$PATH"
model=Cosmos-Embed1-448p-anomaly-detection
cache="$root/models/test-v1/embed-cache"
cp /deploy/rt-embed-checkpoint.json "$root/manifest.json"
jq '.version="test-v1"' "$root/manifest.json" > "$root/updated.json"
mv "$root/updated.json" "$root/manifest.json"
while IFS= read -r name; do
    mkdir -p "$cache/${name%/*}"
    printf abc > "$cache/$name"
done < <(jq -r '.files[].name' "$root/manifest.json")
printf '{"num_video_frames":8,"resolution":448,"embed_dim":768}\n' > "$cache/checkpoint/$model/config.json"
printf '{"weight_map":{"layer":"model-00001-of-00010.safetensors"}}\n' > "$cache/checkpoint/$model/model.safetensors.index.json"
while IFS= read -r name; do
    hash=$(sha256sum "$cache/$name" | cut -d ' ' -f 1)
    bytes=$(stat -c %s "$cache/$name")
    jq --arg name "$name" --arg hash "$hash" --argjson bytes "$bytes" \
       '(.files[] | select(.name==$name)) |= (.sha256=$hash | .bytes=$bytes)' \
       "$root/manifest.json" > "$root/updated.json"
    mv "$root/updated.json" "$root/manifest.json"
done < <(jq -r '.files[].name' "$root/manifest.json")
cp "$root/manifest.json" "$root/good.json"
verify() { bash /deploy/restore-rt-embed-cache.sh "$root/manifest.json" "$root/models"; }
reject() { if verify > "$root/output" 2>&1; then echo "FAIL: $1 accepted"; exit 1; fi; echo "PASS: $1 rejected"; }
asset="$cache/checkpoint/$model/tokenizer.json"
inode=$(stat -c %i "$asset")
[ "$(verify)" = "$cache" ]
[ -f "$root/models/test-v1/.ready" ]
[ "$(stat -c %u:%g "$cache/triton")" = 1001:1001 ]
[ "$(stat -c %i "$asset")" = "$inode" ]
echo 'PASS: complete cache, correct owner, unchanged inode (no copy)'
verify >/dev/null
echo 'PASS: warm cache reuse'
printf abd > "$asset"
reject 'same-size corruption'
[ ! -e "$root/models/test-v1/.ready" ]
printf ab > "$asset"
reject 'partial file'
printf abc > "$asset"
mv "$asset" "$root/asset"
reject 'missing file'
ln -s "$root/asset" "$asset"
reject 'file symlink'
rm "$asset"
mv "$root/asset" "$asset"
mv "$cache/checkpoint" "$root/checkpoint"
ln -s "$root/checkpoint" "$cache/checkpoint"
reject 'symlink ancestor'
rm "$cache/checkpoint"
mv "$root/checkpoint" "$cache/checkpoint"
jq '.files[0].name="../escape"' "$root/good.json" > "$root/manifest.json"
reject 'manifest path traversal'
jq '.files += [.files[0]]' "$root/good.json" > "$root/manifest.json"
reject 'duplicate manifest file'
jq '.files |= map(select(.name | endswith(".engine") | not))' "$root/good.json" > "$root/manifest.json"
reject 'missing engine declaration'
jq '.compatibility.max_batch_size=32' "$root/good.json" > "$root/manifest.json"
reject 'wrong engine batch'
cp "$root/good.json" "$root/manifest.json"
printf '#!/bin/sh\nprintf "NVIDIA H100 80GB HBM3\\n"\n' > "$root/bin/nvidia-smi"
reject 'different GPU variant'
printf '#!/bin/sh\nprintf "NVIDIA H100 NVL\\n"\n' > "$root/bin/nvidia-smi"
verify >/dev/null
echo 'PASS: recovery after validation failures'
