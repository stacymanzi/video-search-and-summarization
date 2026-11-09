#!/usr/bin/env bash
# Run as root in the built deploy image. Fixtures model direct S3 downloads.
set -euo pipefail
restore=${RESTORE_SCRIPT:-/deploy/restore-rt-vlm-model.sh}
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT
model="$work/models/test-v1/test-checkpoint"
mkdir -p "$model"
for file in config.json tokenizer_config.json tokenizer.json preprocessor_config.json; do
    printf '{}\n' > "$model/$file"
done
printf '{"weight_map":{"weight":"model-00001-of-00001.safetensors"}}\n' > "$model/model.safetensors.index.json"
printf 'test weights\n' > "$model/model-00001-of-00001.safetensors"
for file in "$model"/*; do
    digest=$(sha256sum "$file"); digest=${digest%% *}
    jq -n --arg name "${file##*/}" --arg sha256 "$digest" --argjson bytes "$(stat -c %s "$file")" \
        '{name:$name,sha256:$sha256,bytes:$bytes}'
done | jq -s '{directory:"test-checkpoint",version:"test-v1",files:.}' > "$work/manifest.json"
expect_failure() {
    if "$@" >"$work/failure.log" 2>&1; then echo 'FAIL: expected failure'; exit 1; fi
}
expect_failure "$restore" "$work/manifest.json" "$work/missing"
echo 'PASS: missing checkpoint fails'
jq '.files[0].name="../escape"' "$work/manifest.json" > "$work/bad.json"
expect_failure "$restore" "$work/bad.json" "$work/models"
echo 'PASS: unsafe manifest path fails'
inode=$(stat -c %i "$model/model-00001-of-00001.safetensors")
[ "$("$restore" "$work/manifest.json" "$work/models")" = "$model" ]
[ "$(stat -c %i "$model/model-00001-of-00001.safetensors")" = "$inode" ]
[ "$(stat -c %u "$model")" = 1001 ]
[ "$(stat -c %u "$model/.vllm")" = 1001 ]
[ -s "$work/models/test-v1/.ready" ]
echo 'PASS: verified in place, inode unchanged, UID 1001 ownership'
[ "$("$restore" "$work/manifest.json" "$work/models")" = "$model" ]
echo 'PASS: verified warm reuse without extraction or copy'
printf 'TEST weights\n' > "$model/model-00001-of-00001.safetensors"
expect_failure "$restore" "$work/manifest.json" "$work/models"
[ ! -e "$work/models/test-v1/.ready" ]
echo 'PASS: same-size corruption fails and invalidates ready marker'
printf 'partial' > "$model/model-00001-of-00001.safetensors"
expect_failure "$restore" "$work/manifest.json" "$work/models"
echo 'PASS: partial download fails'
mv "$model/model-00001-of-00001.safetensors" "$work/shard-backup"
expect_failure "$restore" "$work/manifest.json" "$work/models"
echo 'PASS: missing shard fails'
ln -s "$work/shard-backup" "$model/model-00001-of-00001.safetensors"
expect_failure "$restore" "$work/manifest.json" "$work/models"
echo 'PASS: symlinked shard fails'
