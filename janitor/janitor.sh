#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# Aetheris Janitor — GFS Backup + 30-Day Media Prune
# TDR §4 & §5: Alpine, GFS Backup Shell, 30-day Pruning Logic
# Persistence: 7 Daily, 2 Weekly, 1 Monthly
# ─────────────────────────────────────────────────────────────────────────────
set -eu

DATA_DIR="${DATA_DIR:-/app/data}"
BACKUP_DIR="${BACKUP_DIR:-/app/backups}"
LOG_DIR="${LOG_DIR:-/app/logs}"
VAULT_DB="${DATA_DIR}/vault/scribe_core.db"

GFS_DAILY_KEEP="${GFS_DAILY_KEEP:-7}"
GFS_WEEKLY_KEEP="${GFS_WEEKLY_KEEP:-2}"
GFS_MONTHLY_KEEP="${GFS_MONTHLY_KEEP:-1}"
PRUNE_MAX_AGE_DAYS="${PRUNE_MAX_AGE_DAYS:-30}"
BACKUP_INTERVAL_HOURS="${BACKUP_INTERVAL_HOURS:-24}"
PRUNE_INTERVAL_HOURS="${PRUNE_INTERVAL_HOURS:-6}"

BACKUP_INTERVAL_SECS=$(( BACKUP_INTERVAL_HOURS * 3600 ))
PRUNE_INTERVAL_SECS=$(( PRUNE_INTERVAL_HOURS * 3600 ))

LOG_FILE="${LOG_DIR}/janitor.log"

# ── Logging ───────────────────────────────────────────────────────────────────
log() {
    echo "[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] [JANITOR] $*" | tee -a "$LOG_FILE"
}

# ── Directory Setup ───────────────────────────────────────────────────────────
mkdir -p "$BACKUP_DIR" "$LOG_DIR"
log "Janitor started. DATA=$DATA_DIR BACKUPS=$BACKUP_DIR"

# ── GFS Backup ────────────────────────────────────────────────────────────────
run_backup() {
    if [ ! -f "$VAULT_DB" ]; then
        log "BACKUP: vault DB not found at $VAULT_DB — skipping."
        return
    fi

    STAMP=$(date -u '+%Y%m%d_%H%M%S')
    DAY=$(date -u '+%u')     # 1=Mon … 7=Sun
    MDAY=$(date -u '+%d')    # day of month (01-31)
    DEST="${BACKUP_DIR}/scribe_core_${STAMP}.db"

    cp "$VAULT_DB" "$DEST"
    log "BACKUP: wrote $DEST"

    # ── GFS Retention ──────────────────────────────────────────────────────────
    # Daily: keep GFS_DAILY_KEEP most-recent files
    ls -1t "${BACKUP_DIR}"/scribe_core_*.db 2>/dev/null \
        | tail -n "+$(( GFS_DAILY_KEEP + 1 ))" \
        | while IFS= read -r old; do
            # Only remove if it's not a designated weekly/monthly keeper
            bname=$(basename "$old")
            # Extract YYYYMMDD from filename
            fdate="${bname#scribe_core_}"
            fdate="${fdate%%_*}"
            fwday=$(date -d "$fdate" '+%u' 2>/dev/null || echo "0")
            fmday=$(date -d "$fdate" '+%d' 2>/dev/null || echo "00")

            is_weekly=0
            is_monthly=0
            [ "$fwday" = "7" ] && is_weekly=1   # Sunday = weekly candidate
            [ "$fmday" = "01" ] && is_monthly=1 # 1st of month = monthly candidate

            if [ "$is_monthly" = "0" ] && [ "$is_weekly" = "0" ]; then
                rm -f "$old"
                log "BACKUP: pruned daily $bname"
            fi
        done

    # Weekly: keep GFS_WEEKLY_KEEP Sunday snapshots
    ls -1t "${BACKUP_DIR}"/scribe_core_*.db 2>/dev/null \
        | while IFS= read -r f; do
            bname=$(basename "$f")
            fdate="${bname#scribe_core_}"; fdate="${fdate%%_*}"
            fwday=$(date -d "$fdate" '+%u' 2>/dev/null || echo "0")
            echo "$fwday $f"
        done \
        | awk '$1=="7"{print $2}' \
        | tail -n "+$(( GFS_WEEKLY_KEEP + 1 ))" \
        | while IFS= read -r old; do
            fmday=$(date -d "$(basename "$old" | sed 's/scribe_core_\([0-9]*\)_.*/\1/')" '+%d' 2>/dev/null || echo "00")
            if [ "$fmday" != "01" ]; then
                rm -f "$old"
                log "BACKUP: pruned weekly $(basename "$old")"
            fi
        done

    # Monthly: keep GFS_MONTHLY_KEEP 1st-of-month snapshots
    ls -1t "${BACKUP_DIR}"/scribe_core_*.db 2>/dev/null \
        | while IFS= read -r f; do
            bname=$(basename "$f")
            fdate="${bname#scribe_core_}"; fdate="${fdate%%_*}"
            fmday=$(date -d "$fdate" '+%d' 2>/dev/null || echo "00")
            echo "$fmday $f"
        done \
        | awk '$1=="01"{print $2}' \
        | tail -n "+$(( GFS_MONTHLY_KEEP + 1 ))" \
        | while IFS= read -r old; do
            rm -f "$old"
            log "BACKUP: pruned monthly $(basename "$old")"
        done

    log "BACKUP: GFS rotation complete."
}

# ── 30-Day Media Prune ────────────────────────────────────────────────────────
run_prune() {
    DELETED=0
    for BUCKET in handouts echo_vault; do
        BUCKET_PATH="${DATA_DIR}/${BUCKET}"
        [ -d "$BUCKET_PATH" ] || continue
        find "$BUCKET_PATH" -type f \( -name "*.png" -o -name "*.mp3" -o -name "*.mp4" \) \
            -mtime "+${PRUNE_MAX_AGE_DAYS}" \
            | while IFS= read -r f; do
                rm -f "$f"
                log "PRUNE: deleted $f"
                DELETED=$(( DELETED + 1 ))
            done
    done
    log "PRUNE: complete."
}

# ── Main Loop ─────────────────────────────────────────────────────────────────
LAST_BACKUP=0
LAST_PRUNE=0

while true; do
    NOW=$(date +%s)

    if [ $(( NOW - LAST_BACKUP )) -ge "$BACKUP_INTERVAL_SECS" ]; then
        run_backup
        LAST_BACKUP=$NOW
    fi

    if [ $(( NOW - LAST_PRUNE )) -ge "$PRUNE_INTERVAL_SECS" ]; then
        run_prune
        LAST_PRUNE=$NOW
    fi

    sleep 300   # check every 5 minutes; actual work fires on interval
done
