#!/usr/bin/env bash
# Run in the deployer image, with no network and no live model volume mounted.
set -euo pipefail
root=$(mktemp -d /tmp/nemotron-cache-test.XXXXXX)
trap 'rm -rf -- "$root"' EXIT
base=ngc/hub/models--nim--nvidia--nvidia-nemotron-nano-9b-v2
blob=0123456789abcdef0123456789abcdef
cache="$root/models/v1/nim-cache"
mkdir -p "$cache/$base/blobs"
printf abc > "$cache/$base/blobs/$blob"
jq -n --arg base "$base" --arg blob "$blob" '{version:"v1", directory:"nim-cache",
  files:[{name:($base+"/blobs/"+$blob), bytes:3,
    sha256:"ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"}],
  links:[{name:($base+"/snapshots/v1/config.json"), target:("../../blobs/"+$blob)}]
}' > "$root/manifest.json"
verify() { bash /deploy/restore-nemotron-cache.sh "$root/manifest.json" "$root/models"; }
reject() { if verify > "$root/output" 2>&1; then echo "FAIL: $1 accepted"; exit 1; fi; echo "PASS: $1 rejected"; }
inode=$(stat -c %i "$cache/$base/blobs/$blob")
[ "$(verify)" = "$cache" ]
[ -f "$root/models/v1/.ready" ]
[ "$(cat "$cache/$base/snapshots/v1/config.json")" = abc ]
[ "$(stat -c %u:%g "$cache")" = 1000:1000 ]
[ "$(stat -c %i "$cache/$base/blobs/$blob")" = "$inode" ]
echo 'PASS: verified files, owner, snapshot links, unchanged inode (no copy)'
verify >/dev/null
echo 'PASS: warm cache reuse'
printf abd > "$cache/$base/blobs/$blob"
reject 'same-size corruption'
[ ! -e "$root/models/v1/.ready" ]
printf abc > "$cache/$base/blobs/$blob"
printf ab > "$cache/$base/blobs/$blob"
reject 'partial download'
printf abc > "$cache/$base/blobs/$blob"
mv "$cache/$base/blobs/$blob" "$root/blob"
reject 'missing blob'
ln -s "$root/blob" "$cache/$base/blobs/$blob"
reject 'blob symlink'
rm "$cache/$base/blobs/$blob"
mv "$root/blob" "$cache/$base/blobs/$blob"
mv "$cache/$base/blobs" "$root/blobs"
ln -s "$root/blobs" "$cache/$base/blobs"
reject 'symlink ancestor'
rm "$cache/$base/blobs"
mv "$root/blobs" "$cache/$base/blobs"
ln -sfn /etc/passwd "$cache/$base/snapshots/v1/config.json"
reject 'incorrect snapshot link'
ln -sfn "../../blobs/$blob" "$cache/$base/snapshots/v1/config.json"
cp "$root/manifest.json" "$root/good.json"
jq '.files[0].name="../escape"' "$root/good.json" > "$root/manifest.json"
reject 'manifest path traversal'
jq '.links[0].target="../../../../etc/passwd"' "$root/good.json" > "$root/manifest.json"
reject 'snapshot path traversal'
jq '.links[0].target="../../blobs/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"' "$root/good.json" > "$root/manifest.json"
reject 'unpinned snapshot target'
cp "$root/good.json" "$root/manifest.json"
verify >/dev/null
echo 'PASS: recovery after validation failures'
