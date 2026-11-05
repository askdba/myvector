#!/usr/bin/env bash
# Overhead of component_mcp_guard on non-MCP traffic (RFC 0001 §10 go/no-go: "guard overhead
# under 1% p99 on non-MCP traffic").
#
# sysbench runs as an ordinary account (no MCP_USER) against pairs of servers from the official
# MySQL image with the same data: in each pair one server never has the guard and the other has
# it installed before the data is loaded. Runs alternate between the two (the other one paused)
# in ABBA order. Every pair is fresh, because a server instance's speed varies from start to
# start by more than the 1% budget, so the result is the median over pairs of each pair's
# overhead. (Installing and uninstalling the guard on one server is not used either: after an
# uninstall the server does not return to its pre-install cost.)
#
# Usage: scripts/bench-mcp-guard.sh <mysql-image> <artifact-dir> [pairs] [rounds] [seconds] [threads]
#   e.g. scripts/bench-mcp-guard.sh mysql:26.10.0 dist/mcp-26.10.0 5 2 30 8
#   WORKLOADS="oltp_point_select" limits the sysbench workloads (default: point select, read/write).
# The artifact dir holds component_mcp_guard.so for that release. Requires Docker. The servers get
# CPUs 0-7 (one loaded at a time) and sysbench CPUs 8-15 when the host has 16 or more.
#
# Per run: TPS, average latency, p99 latency, server CPU per transaction (the change in the
# server cgroup's CPU time over the run, divided by its transactions; it does not depend on
# which side limits throughput) and, when `sudo -n perf` works, the instructions mysqld
# retires per transaction (perf stat on the server process; nearly independent of cache,
# clock and neighbour load, so it shows small per-statement costs that timing hides on a
# shared host). p99 comes from sysbench's latency histogram: its printed
# percentile has two decimals, too coarse for sub-millisecond latencies, and histogram buckets
# are about 1.8% apart, so one run's p99 moves in steps of that size. A p99 change below that is
# reported as not resolvable: this script cannot decide the 1% criterion, only show whether p99
# moved by a bucket or more (exit 1) or not (exit 0).
set -u
IMAGE=${1:?mysql image}; ART=${2:?artifact dir}
PAIRS=${3:-5}; ROUNDS=${4:-2}; SECS=${5:-30}; THREADS=${6:-8}
WORKLOADS=${WORKLOADS:-"oltp_point_select oltp_read_write"}
for n in "$PAIRS" "$ROUNDS" "$SECS" "$THREADS"; do
	case $n in '' | *[!0-9]* | 0*) echo "pairs, rounds, seconds and threads must be positive integers" >&2; exit 1 ;; esac
done
P=mcp-guard-bench-$$
SB_IMAGE=mcp-guard-bench-sysbench
cleanup() { for m in off on; do docker rm -f -v "$P-$m" >/dev/null 2>&1; docker volume rm -f "$P-$m-sock" >/dev/null 2>&1; done; }
trap 'cleanup; rm -f "${RES:-}"' EXIT
if [ "$(nproc)" -ge 16 ]; then SRV_CPUS=(--cpuset-cpus=0-7); SB_CPUS=(--cpuset-cpus=8-15); else SRV_CPUS=(); SB_CPUS=(); fi
printf 'FROM debian:12-slim\nRUN apt-get update -qq && apt-get install -y -qq sysbench >/dev/null && rm -rf /var/lib/apt/lists/*\n' |
	docker build -q -t "$SB_IMAGE" - >/dev/null 2>&1 || { echo "cannot build the sysbench image" >&2; exit 1; }

sql() { docker exec -i "$P-$1" mysql -uroot -pp -N -e "$2" 2>&1 | grep -v "Using a password"; }
# sb MODE ARGS...: sysbench against the server of MODE (off|on) through its Unix socket.
sb() { local m=$1; shift; docker run --rm "${SB_CPUS[@]}" -v "$P-$m-sock:/var/run/mysqld" "$SB_IMAGE" sysbench \
	--db-driver=mysql --mysql-socket=/var/run/mysqld/mysqld.sock --mysql-user=sbtest --mysql-password=sbpass \
	--mysql-db=sbtest --tables=8 --table-size=100000 "$@"; }
# Only the server under test runs: the other is paused, so it takes no CPU.
active() { docker unpause "$P-$1" >/dev/null 2>&1; docker pause "$P-$([ "$1" = on ] && echo off || echo on)" >/dev/null 2>&1; }
# perf runs as "sudo timeout --signal=INT N perf stat", so it always ends (with its counts) even
# when it cannot be stopped early; the probe checks exactly that combination.
if sudo -n timeout --signal=INT 1 perf stat -x, -e instructions -- sleep 5 2>&1 | grep -q ',instructions'; then
	PERF=1
else
	PERF=0
fi
server_cpu_usec() { docker exec "$P-$1" awk '/^usage_usec/ { print $2 }' /sys/fs/cgroup/cpu.stat; }

# start_pair: two fresh servers with the same data, the guard installed on "on".
start_pair() {
	cleanup
	for m in off on; do
		docker volume create "$P-$m-sock" >/dev/null
		docker run -d --name "$P-$m" "${SRV_CPUS[@]}" -v "$P-$m-sock:/var/run/mysqld" -e MYSQL_ROOT_PASSWORD=p \
			"$IMAGE" --innodb-buffer-pool-size=2G --max-connections=500 >/dev/null || return 1
	done
	for m in off on; do
		for _ in $(seq 1 90); do sql "$m" 'select 1' 2>/dev/null | grep -q '^1$' && break; sleep 2; done
		docker exec -u 0 "$P-$m" chmod 755 /var/run/mysqld
		# sbtest has no MCP_USER: its traffic is what the guard must not slow down.
		sql "$m" "CREATE DATABASE sbtest; CREATE USER sbtest@localhost IDENTIFIED BY 'sbpass'; GRANT ALL ON sbtest.* TO sbtest@localhost"
	done
	local pd; pd=$(sql on 'select @@plugin_dir')
	docker cp "$ART/component_mcp_guard.so" "$P-on:$pd/" >/dev/null && docker exec -u 0 "$P-on" chmod 0644 "$pd/component_mcp_guard.so"
	sql on "INSTALL COMPONENT 'file://component_mcp_guard'"
	[ "$(sql on "SELECT COUNT(*) FROM mysql.component WHERE component_urn = 'file://component_mcp_guard'")" = 1 ] || return 1
	[ "$(sql off "SELECT COUNT(*) FROM mysql.component")" = 0 ] || return 1
	for m in off on; do sb "$m" oltp_read_write prepare >/dev/null || return 1; done
}

# run MODE WORKLOAD: a discarded 10 s warm-up (sysbench 1.0 has no --warmup-time), then prints
# "tps avg_ms p99_ms cpu_us_per_txn instructions_per_txn" (0 without perf) of one measured run. Fails if the run produced no result.
run() {
	local m=$1 w=$2 out cpu0 cpu1
	sb "$m" --threads="$THREADS" --time=10 "$w" run >/dev/null 2>&1
	local perf_out instr=0 perf_job=""
	perf_out=$(mktemp)
	cpu0=$(server_cpu_usec "$m")
	if [ "$PERF" = 1 ]; then
		# Counts from just before sysbench starts until it exits (then SIGINT, or the timeout): the window
		# covers the whole measured run; the extra is sysbench's start and connect, the same in
		# both modes, while mysqld is idle.
		sudo -n timeout --signal=INT $((SECS + 30)) perf stat -x, -e instructions \
			-p "$(docker inspect -f '{{.State.Pid}}' "$P-$m")" 2>"$perf_out" &
		perf_job=$!
		sleep 0.5
	fi
	out=$(sb "$m" --threads="$THREADS" --time="$SECS" --histogram=on --percentile=99 "$w" run 2>&1 | awk '
		/transactions:/ { gsub(/[(]/, "", $3); tps = $3 }
		/^ *avg:/ { avg = $2 }
		/^ *[0-9.]+ *\|/ { v[n] = $1; c[n] = $NF; total += $NF; n++ }
		END { want = 0.99 * total; for (i = 0; i < n; i++) { s += c[i]; if (s >= want) { p = v[i]; break } }
		      printf "%s %s %s\n", tps, avg, p }')
	cpu1=$(server_cpu_usec "$m")
	if [ "$PERF" = 1 ]; then
		# Stop it now if we may; otherwise its timeout ends it at most 30 s after the run.
		sudo -n pkill -INT -f "^perf stat -x, -e instructions -p $(docker inspect -f '{{.State.Pid}}' "$P-$m")\$" 2>/dev/null
		wait "$perf_job" 2>/dev/null
		instr=$(awk -F, '$3 ~ /^instructions/ && $1 ~ /^[0-9]+$/ { print $1 }' "$perf_out")
		if ! [ "${instr:-0}" -gt 0 ] 2>/dev/null; then
			echo "perf gave no instruction count for $w on $m: $(head -c 300 "$perf_out")" >&2
			rm -f "$perf_out"
			return 1
		fi
	fi
	rm -f "$perf_out"
	awk -v o="$out" -v d=$((cpu1 - cpu0)) -v t="$SECS" -v i="${instr:-0}" 'BEGIN { split(o, f, " ");
		if (!(f[1] > 0 && f[2] > 0 && f[3] > 0 && d > 0)) exit 1
		printf "%s %s %s %.3f %.0f\n", f[1], f[2], f[3], d / (f[1] * t), i / (f[1] * t) }' || { echo "no result from $w on $m: '$out'" >&2; return 1; }
}
median() { sort -g | awk '{ a[NR] = $1 } END { if (NR == 0) exit 1; printf "%.4f", (NR % 2) ? a[(NR + 1) / 2] : (a[NR / 2] + a[NR / 2 + 1]) / 2 }'; }
# overhead PCT: the guard's overhead in % for a metric (column) of a workload in a pair; TPS
# counts a drop, the others an increase.
overhead() { # pair workload column
	local off on
	off=$(awk -v p="$1" -v w="$2" -v c="$3" '$1 == p && $2 == w && $3 == "off" { print $c }' "$RES" | median)
	on=$(awk -v p="$1" -v w="$2" -v c="$3" '$1 == p && $2 == w && $3 == "on" { print $c }' "$RES" | median)
	if [ "$3" = 4 ]; then awk -v a="$off" -v b="$on" 'BEGIN { printf "%+.2f", (a / b - 1) * 100 }'
	else awk -v a="$off" -v b="$on" 'BEGIN { printf "%+.2f", (b / a - 1) * 100 }'; fi
}

RES=$(mktemp)
echo "== $IMAGE: $PAIRS fresh server pairs x $ROUNDS ABBA rounds, ${SECS}s runs, $THREADS threads, $(sb off --version 2>/dev/null | head -1 || true)"
for pair in $(seq 1 "$PAIRS"); do
	start_pair || { echo "cannot start server pair $pair" >&2; exit 1; }
	for w in $WORKLOADS; do
		for r in $(seq 1 "$ROUNDS"); do
			if [ $((r % 2)) = 1 ]; then order="off on"; else order="on off"; fi
			for m in $order; do
				active "$m"
				out=$(run "$m" "$w") || exit 2
				echo "$pair $w $m $out" | tee -a "$RES" |
					awk '{ printf "  pair %s  %-18s guard %-3s  tps %10.1f  avg %7.3f ms  p99 %7.3f ms  cpu %8.3f us/txn  instr %9d/txn\n", $1, $2, $3, $4, $5, $6, $7, $8 }'
			done
		done
	done
done
echo "== guard overhead per pair (median of its runs), then the median over pairs"
P99_RESOLUTION=1.8
above=0
for w in $WORKLOADS; do
	cols="4 5 6 7"; [ "$PERF" = 1 ] && cols="$cols 8"
	for col in $cols; do
		name=$(case $col in 4) echo tps ;; 5) echo "avg latency" ;; 6) echo "p99 latency" ;; 7) echo "cpu/txn" ;; 8) echo "instr/txn" ;; esac)
		per=""; for pair in $(seq 1 "$PAIRS"); do per="$per $(overhead "$pair" "$w" "$col")"; done
		med=$(printf '%s\n' $per | median)
		printf '  %-18s %-12s pairs:%s  median %+.2f%%\n' "$w" "$name" "$(printf ' %s%%' $per)" "$med"
		if [ "$col" = 6 ] && awk -v p="$med" -v r="$P99_RESOLUTION" 'BEGIN { exit !(p >= r) }'; then above=1; fi
	done
done
# sysbench 1.0's histogram buckets are about 1.8% apart: a p99 change smaller than a bucket is
# not resolved (both modes can fall in the same bucket), so the 1% criterion is never "passed"
# here; it needs a client that records exact per-query latencies.
if [ "$above" = 1 ]; then echo "== median p99 overhead at or above the ${P99_RESOLUTION}% histogram resolution on a workload"; exit 1; fi
echo "== median p99 overhead below the ${P99_RESOLUTION}% histogram resolution: not resolvable to the 1% criterion with sysbench"
exit 0
