#!/usr/bin/env bash
# Verify direct S3 downloads and recreate NIM snapshot links; never copy weights.
set -euo pipefail
manifest=${1:-/deploy/nemotron-checkpoint.json}
model_root=${2:-/models/nemotron}
log() { echo "[nemotron-cache] $*" >&2; }
fail() { log "ERROR: $*"; exit 1; }
[[ "$model_root" = /* && "$model_root" != / ]] || fail 'model root must be an absolute non-root path'
version=$(jq -er '.version' "$manifest")
name=$(jq -er '.directory' "$manifest")
[[ "$version" =~ ^[A-Za-z0-9_-]+$ && "$name" =~ ^[A-Za-z0-9_-]+$ ]] || fail 'invalid cache version or directory'
base='ngc/hub/models--nim--nvidia--nvidia-nemotron-nano-9b-v2'
jq -e --arg base "$base" '
  (.files | type == "array" and length > 0) and
  (all(.files[];
    (.name | test("^" + $base + "/(blobs/[a-f0-9]{32}(-[0-9]+)?|refs/[A-Za-z0-9_-][A-Za-z0-9_.-]*)$")) and
    (.sha256 | test("^[a-f0-9]{64}$")) and
    (.bytes | type == "number" and . > 0 and . == floor))) and
  ((.files | map(.name) | unique | length) == (.files | length)) and
  (.links | type == "array" and length > 0) and
  (all(.links[];
    (.name | test("^" + $base + "/snapshots/[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*$")) and
    (.target | test("^\\.\\./\\.\\./blobs/[a-f0-9]{32}(-[0-9]+)?$")))) and
  ((.links | map(.name) | unique | length) == (.links | length))
' "$manifest" >/dev/null || fail 'invalid cache manifest'
destination="${model_root}/${version}"
cache="${destination}/${name}"

# Reject symlink ancestors too, not just the final file. Do this before mkdir,
# hashing or chown so a malformed local cache cannot redirect those operations.
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
while IFS=$'\t' read -r file bytes; do
    safe_path "${cache}/${file}"
    [ -f "${cache}/${file}" ] || fail "missing cache file: ${file}"
    [ "$(stat -c %s "${cache}/${file}")" = "$bytes" ] || fail "wrong size: ${file}; quarantine the incomplete file before retrying the loader"
done < <(jq -r '.files[] | [.name,.bytes] | @tsv' "$manifest")
log 'verifying direct S3 Nemotron files (four checksum workers; no extraction or copy)'
(
    cd "$cache"
    jq -r '.files[] | [.sha256,.name] | @tsv' "$manifest" |
        xargs -n 2 -P 4 bash -c 'printf "%s  %s\n" "$1" "$2" | sha256sum --check --status' _
) || fail 'cache checksum mismatch; quarantine the bad file before retrying the loader'

while IFS=$'\t' read -r link target; do
    blob="${base}/blobs/${target##*/}"
    jq -e --arg name "$blob" 'any(.files[]; .name == $name)' "$manifest" >/dev/null || fail 'snapshot target is not checksum-pinned'
    parent="${cache}/${link%/*}"
    safe_path "$parent"
    mkdir -p "$parent"
    if [ -L "${cache}/${link}" ]; then
        [ "$(readlink "${cache}/${link}")" = "$target" ] || fail "incorrect snapshot link: ${link}"
    else
        [ ! -e "${cache}/${link}" ] || fail "snapshot path is not a link: ${link}"
        ln -s "$target" "${cache}/${link}"
    fi
done < <(jq -r '.links[] | [.name,.target] | @tsv' "$manifest")

# Only touch pinned assets and their parents. NIM creates its own runtime cache;
# do not walk or upload that cache, locks, credentials, or generated Python code.
while IFS= read -r file; do
    chmod 644 "${cache}/${file}"
    chown 1000:1000 "${cache}/${file}"
done < <(jq -r '.files[].name' "$manifest")
while IFS= read -r file; do
    parent="${cache}/${file%/*}"
    while [[ "$parent" = "$cache" || "$parent" = "$cache/"* ]]; do
        chmod 755 "$parent"
        chown 1000:1000 "$parent"
        parent=${parent%/*}
    done
done < <(jq -r '.files[].name, .links[].name' "$manifest")
chmod 755 "$destination"
marker=$(mktemp "${destination}/.ready.XXXXXX")
sha256sum "$manifest" | cut -d ' ' -f 1 > "$marker"
mv -T "$marker" "${destination}/.ready"
log "direct-file Nemotron cache ready: ${cache}"
printf '%s\n' "$cache"
