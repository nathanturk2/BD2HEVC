#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "usage: verify_udf_tail.sh IMAGE SOURCE_FILE IMAGE_FILE_PATH" >&2
    exit 2
fi

image=$1
source_file=$2
image_file_path=$3
mount_point=/mnt/wsl/bd2hevc_udf_tail_check

mkdir -p "$mount_point"
cleanup() {
    umount "$mount_point" 2>/dev/null || true
}
trap cleanup EXIT

mount -o loop,ro "$image" "$mount_point"
source_hash=$(tail -c 1048576 "$source_file" | sha256sum | cut -d' ' -f1)
image_hash=$(tail -c 1048576 "$mount_point/$image_file_path" | sha256sum | cut -d' ' -f1)
source_size=$(stat -c '%s' "$source_file")
image_size=$(stat -c '%s' "$mount_point/$image_file_path")

printf 'SOURCE_TAIL_SHA256=%s\n' "$source_hash"
printf 'IMAGE_TAIL_SHA256=%s\n' "$image_hash"
printf 'SOURCE_SIZE=%s\n' "$source_size"
printf 'IMAGE_SIZE=%s\n' "$image_size"
test "$source_hash" = "$image_hash"
test "$source_size" = "$image_size"
