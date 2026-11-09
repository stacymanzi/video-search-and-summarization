#!/usr/bin/env bash
# Verify a pinned checkpoint downloaded directly into vss_models. No extraction.
set -euo pipefail
manifest=${1:-/deploy/rt-vlm-checkpoint.json}
model_root=${2:-/models/rt-vlm}
log() { echo "[rt-vlm-restore] $*" >&2; }
fail() { log "ERROR: $*"; exit 1; }
case "$model_root" in /*) ;; *) fail 'model root must be absolute';; esac
[ "$model_root" != / ] || fail 'model root cannot be /'
name=$(jq -er '.directory' "$manifest")
version=$(jq -er '.version' "$manifest")
[[ "$name" =~ ^[A-Za-z0-9_-]+$ && "$version" =~ ^[A-Za-z0-9_-]+$ ]] || fail 'invalid model directory or version'
jq -e '.files | (type == "array" and length > 0) and
    (all(.[]; (.name | test("^[A-Za-z0-9_-][A-Za-z0-9_.-]*$")) and
               (.sha256 | test("^[a-f0-9]{64}$")) and
               (.bytes | type == "number" and . > 0 and . == floor))) and
    ((map(.name) | unique | length) == length)' "$manifest" >/dev/null || fail 'invalid file manifest'
destination="${model_root}/${version}"
checkpoint="${destination}/${name}"
mkdir -p "$model_root"
exec 9>"${model_root}/.restore.lock"
flock -x 9
[ -d "$checkpoint" ] && [ ! -L "$destination" ] && [ ! -L "$checkpoint" ] || fail 'checkpoint directory missing or unsafe; the S3 loader must finish first'

# A ready marker never bypasses verification. On failure remove only the marker;
# preserve model files for diagnosis. Files are never copied or extracted here.
trap 'rc=$?; if [ "$rc" -ne 0 ]; then rm -f -- "${destination}/.ready"; fi' EXIT
while IFS=$'\t' read -r filename bytes; do
    file="${checkpoint}/${filename}"
    [ -f "$file" ] && [ ! -L "$file" ] || fail "missing or unsafe model file: ${filename}"
    [ "$(stat -c %s "$file")" = "$bytes" ] || fail "wrong size: ${filename}; quarantine the incomplete file before restarting the loader"
done < <(jq -r '.files[] | [.name,.bytes] | @tsv' "$manifest")
log 'verifying direct S3 model files (four checksum workers; no extraction or copy)'
(
    cd "$checkpoint"
    jq -r '.files[] | [.sha256,.name] | @tsv' "$manifest" |
        xargs -n 2 -P 4 bash -c 'printf "%s  %s\n" "$1" "$2" | sha256sum --check --status' _
) || fail 'model checksum mismatch; quarantine the bad file before restarting the loader'

validate_model() {
    local directory=$1 shard
    for asset in config.json tokenizer_config.json tokenizer.json preprocessor_config.json model.safetensors.index.json; do
        jq -e --arg name "$asset" 'any(.files[]; .name == $name)' "$manifest" >/dev/null || fail "manifest missing ${asset}"
    done
    jq -e '.weight_map | type == "object" and length > 0' "${directory}/model.safetensors.index.json" >/dev/null
    while IFS= read -r shard; do
        [[ "$shard" =~ ^[A-Za-z0-9_-]+\.safetensors$ ]] || fail 'invalid weight shard path'
        [ -s "${directory}/${shard}" ] || fail "checkpoint missing shard ${shard}"
        jq -e --arg name "$shard" 'any(.files[]; .name == $name)' "$manifest" >/dev/null || fail "unverified weight shard: ${shard}"
    done < <(jq -r '.weight_map | [.[]] | unique[]' "${directory}/model.safetensors.index.json")
}

validate_model "$checkpoint"
# RT-VLM writes .lock and .vllm beside the weights. Never traverse runtime files
# or modify another cache. The version directory and marker remain root-owned.
[ ! -L "${checkpoint}/.vllm" ] || fail 'unsafe compilation cache path'
mkdir -p "${checkpoint}/.vllm"
chown 1001:1001 "$checkpoint" "${checkpoint}/.vllm"
chmod 755 "$destination" "$checkpoint" "${checkpoint}/.vllm"
while IFS= read -r filename; do
    chown 1001:1001 "${checkpoint}/${filename}"
    chmod 644 "${checkpoint}/${filename}"
done < <(jq -r '.files[].name' "$manifest")
marker=$(mktemp "${destination}/.ready.XXXXXX")
sha256sum "$manifest" | cut -d ' ' -f 1 > "$marker"
mv -T "$marker" "${destination}/.ready"
log "direct-file checkpoint ready: ${checkpoint}"
printf '%s\n' "$checkpoint"
