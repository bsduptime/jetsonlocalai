#!/usr/bin/env bash
# Benchmark sequential throughput + random latency for given mountpoints using fio.
# --direct=1 bypasses the page cache, so results reflect the device, not RAM. No sudo needed.
set -uo pipefail

TARGETS=("$@")
[[ ${#TARGETS[@]} -eq 0 ]] && TARGETS=(/mnt/transcend /mnt/sdcard)

run_fio() {  # name rw bs size extra...
  local name=$1 rw=$2 bs=$3 size=$4; shift 4
  fio --name="$name" --filename="$DIR/.fiobench" --rw="$rw" --bs="$bs" \
      --size="$size" --direct=1 --ioengine=psync --group_reporting \
      --output-format=json "$@" 2>/dev/null
}

jget() { python3 -c "import sys,json;d=json.load(sys.stdin);print(eval('d'+sys.argv[1]))" "$1"; }

for DIR in "${TARGETS[@]}"; do
  echo "================================================================"
  echo "TARGET: $DIR   ($(findmnt -no SOURCE "$DIR" 2>/dev/null) / $(findmnt -no FSTYPE "$DIR" 2>/dev/null))"
  echo "================================================================"
  if ! touch "$DIR/.fiowtest" 2>/dev/null; then echo "  NOT WRITABLE — skipping"; continue; fi
  rm -f "$DIR/.fiowtest"

  # 1. Sequential WRITE (1 GiB, 1M blocks)
  o=$(run_fio seqwrite write 1M 1G --end_fsync=1)
  bw=$(echo "$o" | jget "['jobs'][0]['write']['bw_bytes']")
  printf "  Seq write : %6.1f MB/s\n" "$(awk "BEGIN{print $bw/1e6}")"

  # 2. Sequential READ (1 GiB)
  o=$(run_fio seqread read 1M 1G)
  bw=$(echo "$o" | jget "['jobs'][0]['read']['bw_bytes']")
  printf "  Seq read  : %6.1f MB/s\n" "$(awk "BEGIN{print $bw/1e6}")"

  # 3. Random READ 4k, depth1, latency-focused (15s)
  o=$(run_fio randread randread 4k 512M --iodepth=1 --runtime=15 --time_based)
  iops=$(echo "$o" | jget "['jobs'][0]['read']['iops']")
  lat=$(echo "$o" | jget "['jobs'][0]['read']['clat_ns']['mean']")
  printf "  Rand read : %6.0f IOPS  | avg latency %7.3f ms\n" "$iops" "$(awk "BEGIN{print $lat/1e6}")"

  # 4. Random WRITE 4k, depth1 (15s)
  o=$(run_fio randwrite randwrite 4k 512M --iodepth=1 --runtime=15 --time_based)
  iops=$(echo "$o" | jget "['jobs'][0]['write']['iops']")
  lat=$(echo "$o" | jget "['jobs'][0]['write']['clat_ns']['mean']")
  printf "  Rand write: %6.0f IOPS  | avg latency %7.3f ms\n" "$iops" "$(awk "BEGIN{print $lat/1e6}")"

  rm -f "$DIR/.fiobench"
done
echo "================================================================"
echo "done"
