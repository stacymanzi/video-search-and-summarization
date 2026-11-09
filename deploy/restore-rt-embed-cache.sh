#!/usr/bin/env bash
# Verify a complete RT-Embed checkpoint + Triton repository downloaded from S3.
# No extraction/copy, no deletion of old Docker caches, no network access.
set -euo pipefail
manifest=${1:-/deploy/rt-embed-checkpoint.json}
model_root=${2:-/models/rt-embed}
log() { echo "[rt-embed-cache] $*" >&2; }
fail() { log "ERROR: $*"; exit 1; }
[[ "$model_root" = /* && "$model_root" != / ]] || fail 'model root must be an absolute non-root path'
version=$(jq -er '.version' "$manifest")
name=$(jq -er '.directory' "$manifest")
model=$(jq -er '.model_directory' "$manifest")
repo=$(jq -er '.repository_directory' "$manifest")
for component in "$version" "$name" "$model" "$repo"; do
    [[ "$component" =~ ^[A-Za-z0-9_-]+$ ]] || fail 'invalid cache directory'
done
jq -e --arg model "$model" --arg repo "$repo" '
  (.image | test("^nvcr.io/nvidia/vss-core/vss-rt-embed@sha256:[a-f0-9]{64}$")) and
  (.source_revision | test("^[a-f0-9]{40}$")) and
  (.files | type == "array" and length > 0) and
  (all(.files[];
    (.name | test("^(checkpoint/" + $model + "|triton/" + $repo + ")/([A-Za-z0-9_-][A-Za-z0-9_.-]*/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*$")) and
    (.sha256 | test("^[a-f0-9]{64}$")) and
    (.bytes | type == "number" and . > 0 and . == floor))) and
  ((.files | map(.name) | unique | length) == (.files | length)) and
  (.compatibility.gpu_name == "NVIDIA H100 NVL") and
  (.compatibility.architecture == "x86_64") and
  (.compatibility.max_batch_size == 64) and
  (.compatibility.precision == "fp16") and
  (.compatibility.extra_trtexec_args == "") and
  (.compatibility.num_video_frames == 8) and
  (.compatibility.resolution == 448) and
  (.compatibility.embed_dim == 768)
' "$manifest" >/dev/null || fail 'invalid or unsupported cache manifest'
destination="${model_root}/${version}"
cache="${destination}/${name}"
safe_path() {
    local path=$1 current='' part
    local -a parts
    IFS=/ read -r -a parts <<< "$path"
    for part in "${parts[@]}"; do
        [ -n "$part" ] || continue
        [[ "$part" != . && "$part" != .. ]] || fail 'unsafe path component'
        current="${current}/${part}"
        [ ! -L "$current" ] || fail "symlink in cache path: ${current}"
    done
}
safe_path "$cache"
[ -d "$cache" ] || fail 'cache missing; the S3 loader must finish first'
safe_path "${model_root}/.restore.lock"
exec 9>"${model_root}/.restore.lock"
flock -x 9
trap 'rc=$?; if [ "$rc" -ne 0 ]; then rm -f -- "${destination}/.ready"; fi' EXIT

# The digest pins TensorRT/CUDA and the builder implementation. The course uses
# homogeneous GPUs; reject a mixed/unknown host before any deployment teardown.
[ "$(uname -m)" = "$(jq -r '.compatibility.architecture' "$manifest")" ] || fail 'CPU architecture does not match cached engine'
gpu_names=$(nvidia-smi --query-gpu=name --format=csv,noheader) || fail 'cannot verify GPU compatibility'
[ -n "$gpu_names" ] || fail 'no GPUs visible'
while IFS= read -r gpu; do
    [ "$gpu" = "$(jq -r '.compatibility.gpu_name' "$manifest")" ] || fail "GPU ${gpu} does not match cached engine"
done <<< "$gpu_names"

require_pinned() {
    jq -e --arg file "$1" 'any(.files[]; .name == $file)' "$manifest" >/dev/null || fail "required asset is not pinned: $1"
}
for file in config.json model.safetensors.index.json tokenizer_config.json tokenizer.json vocab.txt \
            configuration_embed1.py preprocessing_embed1.py modeling_embed1.py; do
    require_pinned "checkpoint/${model}/${file}"
done
for branch in video text; do
    for file in config.pbtxt "cosmos_embed1_${branch}.onnx" "cosmos_embed1_${branch}.onnx.data" \
                "1/cosmos_embed1_${branch}_NVIDIA_H100_NVL_64_fp16.engine"; do
        require_pinned "triton/${repo}/${branch}_embeddings/${file}"
    done
done
while IFS=$'\t' read -r file bytes; do
    safe_path "${cache}/${file}"
    [ -f "${cache}/${file}" ] || fail "missing cache file: ${file}"
    [ "$(stat -c %s "${cache}/${file}")" = "$bytes" ] || fail "wrong size: ${file}; quarantine the incomplete file before retrying the loader"
done < <(jq -r '.files[] | [.name,.bytes] | @tsv' "$manifest")
log 'verifying RT-Embed checkpoint, ONNX exports and engines (four checksum workers; no extraction or copy)'
(
    cd "$cache"
    jq -r '.files[] | [.sha256,.name] | @tsv' "$manifest" |
        xargs -n 2 -P 4 bash -c 'printf "%s  %s\n" "$1" "$2" | sha256sum --check --status' _
) || fail 'cache checksum mismatch; quarantine the bad file before retrying the loader'
jq -e '.num_video_frames == 8 and .resolution == 448 and .embed_dim == 768' \
    "${cache}/checkpoint/${model}/config.json" >/dev/null || fail 'model configuration does not match engine'
jq -e '.weight_map | type == "object" and length > 0' \
    "${cache}/checkpoint/${model}/model.safetensors.index.json" >/dev/null || fail 'invalid shard index'
while IFS= read -r shard; do
    [[ "$shard" =~ ^[A-Za-z0-9_-]+\.safetensors$ ]] || fail 'invalid weight shard path'
    require_pinned "checkpoint/${model}/${shard}"
done < <(jq -r '.weight_map | [.[]] | unique[]' "${cache}/checkpoint/${model}/model.safetensors.index.json")

# Upstream regenerates config.pbtxt on startup. Preserve a writable Triton repo
# for UID 1001; touch only pinned files and their parents, not runtime HF caches.
while IFS= read -r file; do
    chmod 644 "${cache}/${file}"
    chown 1001:1001 "${cache}/${file}"
    parent="${cache}/${file%/*}"
    while [[ "$parent" = "$cache" || "$parent" = "$cache/"* ]]; do
        chmod 755 "$parent"
        chown 1001:1001 "$parent"
        parent=${parent%/*}
    done
done < <(jq -r '.files[].name' "$manifest")
chmod 755 "$destination"
marker=$(mktemp "${destination}/.ready.XXXXXX")
sha256sum "$manifest" | cut -d ' ' -f 1 > "$marker"
mv -T "$marker" "${destination}/.ready"
log "direct-file RT-Embed cache ready: ${cache}"
printf '%s\n' "$cache"
