#!/usr/bin/env bash
# =============================================================================
# mount_gdrive.sh  –  Mount Google Drive via Rclone (Linux / WSL / macOS)
# =============================================================================
# Prerequisites:
#   1. rclone installed  →  https://rclone.org/install/
#   2. rclone remote configured  →  run:  rclone config
#      • Choose "Google Drive", name the remote:  lsse_gdrive
# =============================================================================

set -euo pipefail

REMOTE_NAME="lsse_gdrive"          # must match what you named it in rclone config
MOUNT_POINT="$(pwd)/gdrive_mount"  # local folder that acts as the drive root

# ── Create mount point if it does not exist ───────────────────────────────────
mkdir -p "$MOUNT_POINT"

echo "[LSSE] Mounting Google Drive → $MOUNT_POINT"
echo "[LSSE] Press Ctrl-C to unmount."

rclone mount "${REMOTE_NAME}:" "$MOUNT_POINT" \
    --vfs-cache-mode full \
    --vfs-cache-max-size 100G \
    --tpslimit 10 \
    --drive-chunk-size 64M \
    --allow-non-empty \
    --daemon

echo "[LSSE] Google Drive mounted at: $MOUNT_POINT"
echo "[LSSE] To unmount run:  fusermount -u $MOUNT_POINT   (Linux)"
echo "                    or:  umount $MOUNT_POINT          (macOS)"
