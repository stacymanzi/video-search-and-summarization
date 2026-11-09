#!/usr/bin/env bash
# Validate direct S3 files; restore only the small runtime marker and alias.
set -euo pipefail
manifest=${1:-/deploy/rt-cv-checkpoint.json}
model_root=${2:-/models/rt-cv}
log() { echo "[rt-cv-cache] $*" >&2; }
fail() { log "ERROR: $*"; exit 1; }
[[ "$model_root" = /* && "$model_root" != / ]] || fail 'model root must be absolute and non-root'
version=$(jq -er '.version' "$manifest")
name=$(jq -er '.directory' "$manifest")
[[ "$version" =~ ^[A-Za-z0-9_-]+$ && "$name" =~ ^[A-Za-z0-9_-]+$ ]] || fail 'invalid directory'
jq -e '
  (.image | test("^nvcr.io/nvidia/vss-core/vss-rt-cv@sha256:[a-f0-9]{64}$")) and
  (.files | type == "array" and length == 11) and
  (all(.files[];
    (.name | test("^([A-Za-z0-9_-][A-Za-z0-9_.-]*/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*$")) and
    (.sha256 | test("^[a-f0-9]{64}$")) and
    (.bytes | type == "number" and . > 0 and . == floor))) and
  ((.files | map(.name) | unique | length) == (.files | length)) and
  (.compatibility.gpu_name == "NVIDIA H100 NVL") and
  (.compatibility.architecture == "x86_64") and
  (.compatibility.detector_batch == 16) and
  (.compatibility.vision_encoder_batch == 128) and
  (.compatibility.detector_input == "3;544;960")
' "$manifest" >/dev/null || fail 'invalid or unsupported manifest'
cache="${model_root}/${version}/${name}"
safe_path() {
    local path=$1 current='' part
    local -a parts
    IFS=/ read -r -a parts <<< "$path"
    for part in "${parts[@]}"; do
        [ -n "$part" ] || continue
        [[ "$part" != . && "$part" != .. ]] || fail 'unsafe path component'
        current="${current}/${part}"
        [ ! -L "$current" ] || fail "symlink in cache path: $current"
    done
}
safe_path "$cache"
[ -d "$cache" ] || fail 'cache missing; S3 loader must finish first'
safe_path "${model_root}/.restore.lock"
exec 9>"${model_root}/.restore.lock"
flock -x 9
[ "$(uname -m)" = x86_64 ] || fail 'CPU architecture mismatch'
gpus=$(nvidia-smi --query-gpu=name --format=csv,noheader) || fail 'cannot verify GPU'
[ -n "$gpus" ] || fail 'no GPUs'
while IFS= read -r gpu; do
    [ "$gpu" = 'NVIDIA H100 NVL' ] || fail "incompatible GPU: $gpu"
done <<< "$gpus"
onnx=siglip_v2_v1.1.onnx
engine=${onnx}_batch16.plan
detector=rtdetr-its/model_epoch_035.fp16.onnx
for file in "$onnx" "$engine" "$engine.meta" siglip_v2_v1.1_weights.bin \
    "$detector" "${detector}_b16_gpu0_fp16.engine" \
    siglip_v2_v1.1_tokenizer/{tokenizer.json,tokenizer.model,tokenizer_config.json,special_tokens_map.json,chat_template.jinja}; do
    jq -e --arg file "$file" 'any(.files[]; .name == $file)' "$manifest" >/dev/null || fail "required asset not pinned: $file"
done
while IFS=$'\t' read -r file bytes; do
    safe_path "$cache/$file"
    [ -f "$cache/$file" ] || fail "missing file: $file"
    [ "$(stat -c %s "$cache/$file")" = "$bytes" ] || fail "wrong size: $file"
done < <(jq -r '.files[] | [.name,.bytes] | @tsv' "$manifest")
log 'verifying RT-CV models and engines (four checksum workers; no extraction or copy)'
(
    cd "$cache"
    jq -r '.files[] | [.sha256,.name] | @tsv' "$manifest" |
        xargs -n 2 -P 4 bash -c 'printf "%s  %s\n" "$1" "$2" | sha256sum --check --status' _
) || fail 'checksum mismatch; quarantine the bad file before retrying the loader'
[ "$(cat "$cache/$engine.meta")" = "/opt/storage/$onnx" ] || fail 'SigLIP engine source path mismatch'

# S3 download order/mtime is not model provenance. Normalize only after hashes
# prove that this ONNX and engine are the pinned pair. Never rewrite model data.
touch -r "$cache/$onnx" "$cache/$engine"
alias_path="$cache/siglip2_v1.1_weights.bin"
if [ -L "$alias_path" ]; then
    [ "$(readlink "$alias_path")" = siglip_v2_v1.1_weights.bin ] || fail 'unexpected weights alias'
elif [ -e "$alias_path" ]; then
    fail 'weights alias exists but is not the expected symlink'
else
    ln -s siglip_v2_v1.1_weights.bin "$alias_path"
fi
marker="$cache/.siglip_v2_v1.1.done"
safe_path "$marker"
[ ! -e "$marker" ] || [ -f "$marker" ] || fail 'invalid init marker'
touch "$marker"
chmod 644 "$marker"
# RT-CV runs as UID 1000; unlike RT-Embed, it does not run as 1001.
while IFS= read -r file; do
    chmod 644 "$cache/$file"
    chown 1000:1000 "$cache/$file"
    parent=$(dirname "$cache/$file")
    while [[ "$parent" = "$cache" || "$parent" = "$cache/"* ]]; do
        chmod 755 "$parent"
        chown 1000:1000 "$parent"
        parent=${parent%/*}
    done
done < <(jq -r '.files[].name' "$manifest")
chmod 755 "${model_root}/${version}"
log "direct-file RT-CV cache ready: $cache"
printf '%s\n' "$cache"
