#!/usr/bin/env bash
# Benchmark for issue #79: myvector_distance with nested myvector_construct.
# Compares: nested myvector_construct (slow) vs precomputed 0x literal (fast).
#
# Usage:
#   ./scripts/benchmark-issue79.sh                    # Use prebuilt image
#   ./scripts/benchmark-issue79.sh /path/to/myvector.so  # Use custom plugin
#
# Env: BENCH_ROWS (default 1000), BENCH_DIM (default 768), BENCH_RUNS (default 3)
#      768-dim matches issue #79 (CLIP). Use BENCH_DIM=3 for quick runs.
#
# Output: Timing for nested query and literal query, ratio.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

IMAGE="${MYVECTOR_IMAGE:-ghcr.io/askdba/myvector:mysql8.4}"
CONTAINER="myvector-bench-$$"
PLUGIN_SO="${1:-}"
ROWS="${BENCH_ROWS:-1000}"
RUNS="${BENCH_RUNS:-3}"
DIM="${BENCH_DIM:-768}"

# 768-dim matches issue #79 (CLIP vectors). 3-dim is too cheap to show the problem.
echo "=== Issue #79 Benchmark (rows=$ROWS, dim=$DIM, runs=$RUNS) ==="

cleanup() {
	docker rm -f "$CONTAINER" 2>/dev/null || true
}
trap cleanup EXIT

docker run -d --name "$CONTAINER" \
	-e MYSQL_ROOT_PASSWORD=bench \
	-e MYSQL_DATABASE=bench \
	-e MYSQL_ROOT_HOST=% \
	"$IMAGE"

echo "Waiting for MySQL..."
for i in $(seq 1 60); do
	docker exec "$CONTAINER" mysql -uroot -pbench -e "SELECT 1" 2>/dev/null && break
	sleep 2
done
docker exec "$CONTAINER" mysql -uroot -pbench -e "SELECT 1" || {
	echo "MySQL not ready"
	exit 1
}

if [ -n "$PLUGIN_SO" ] && [ -f "$PLUGIN_SO" ]; then
	echo "Installing custom plugin from $PLUGIN_SO"
	PLUGIN_DIR=$(docker exec "$CONTAINER" mysql -uroot -pbench -N -e "SELECT @@plugin_dir;")
	docker cp "$PLUGIN_SO" "$CONTAINER:$PLUGIN_DIR/myvector.so"
	docker exec "$CONTAINER" mysql -uroot -pbench -e "UNINSTALL PLUGIN myvector; INSTALL PLUGIN myvector SONAME 'myvector.so';"
	docker exec "$CONTAINER" mysql -uroot -pbench -e "SOURCE /docker-entrypoint-initdb.d/myvectorplugin.sql;" 2>/dev/null || true
fi

# Generate N-dim vector string via Python (fast)
gen_vec() {
	python3 -c "
import sys
n, dim = int(sys.argv[1]), int(sys.argv[2])
vals = [((n + i) * 0.001 - 0.5) for i in range(dim)]
print('[' + ','.join(str(v) for v in vals) + ']')
" "$1" "$DIM"
}

echo "Creating table with $ROWS rows (${DIM}-dim vectors)..."
VARSIZE=$((DIM * 4 + 16))
docker exec "$CONTAINER" mysql -uroot -pbench bench -e "
  DROP TABLE IF EXISTS t79;
  CREATE TABLE t79 (id INT PRIMARY KEY, v VARBINARY($VARSIZE));
"

# Query vector for nested/literal (constant across all rows)
QUERY_VEC=$(gen_vec 1)

# Insert in batches of 5 for large vectors
for ((i = 1; i <= ROWS; i += 5)); do
	end=$((i + 4))
	[ "$end" -gt "$ROWS" ] && end=$ROWS
	vals=""
	for ((n = i; n <= end; n++)); do
		vec=$(gen_vec "$n")
		vec_escaped="${vec//\'/\\\'}"
		[ -n "$vals" ] && vals="$vals,"
		vals="${vals}($n, myvector_construct('$vec_escaped'))"
	done
	docker exec "$CONTAINER" mysql -uroot -pbench bench -e "INSERT INTO t79 VALUES $vals" 2>/dev/null
done

run_timed() {
	local q="$1"
	local start end
	start=$(python3 -c "import time; print(int(time.time()*1e6))" 2>/dev/null || echo $(($(date +%s) * 1000000)))
	docker exec "$CONTAINER" mysql -uroot -pbench bench -N -e "$q" 2>/dev/null >/dev/null
	end=$(python3 -c "import time; print(int(time.time()*1e6))" 2>/dev/null || echo $(($(date +%s) * 1000000)))
	echo $((end - start))
}

# Escape single quotes for SQL
QUERY_VEC_SQL="${QUERY_VEC//\'/\'\'}"
echo ""
echo "--- Nested myvector_construct (issue #79 pattern) ---"
nested_times=()
for r in $(seq 1 "$RUNS"); do
	t=$(run_timed "SELECT COUNT(*) FROM (SELECT myvector_distance(v, myvector_construct('$QUERY_VEC_SQL'), 'L2') AS d FROM t79) sub")
	nested_times+=("$t")
	echo "  Run $r: ${t} µs"
done

echo ""
echo "--- Precomputed 0x literal (baseline) ---"
LIT_HEX=$(docker exec "$CONTAINER" mysql -uroot -pbench -N -e "SELECT HEX(myvector_construct('$QUERY_VEC_SQL')) FROM DUAL;" 2>/dev/null)
literal_times=()
for r in $(seq 1 "$RUNS"); do
	t=$(run_timed "SELECT COUNT(*) FROM (SELECT myvector_distance(v, 0x$LIT_HEX, 'L2') AS d FROM t79) sub")
	literal_times+=("$t")
	echo "  Run $r: ${t} µs"
done

# Compute average
nested_sum=0
literal_sum=0
for t in "${nested_times[@]}"; do nested_sum=$((nested_sum + t)); done
for t in "${literal_times[@]}"; do literal_sum=$((literal_sum + t)); done
nested_avg=$((nested_sum / RUNS))
literal_avg=$((literal_sum / RUNS))
if [ "$literal_avg" -gt 0 ]; then
	ratio=$(echo "scale=2; $nested_avg / $literal_avg" | bc 2>/dev/null || echo "N/A")
	[[ "$ratio" = .* ]] && ratio="0$ratio"
else
	ratio="N/A"
fi

echo ""
echo "=== Results ==="
echo "Nested myvector_construct: ${nested_avg} µs (avg)"
echo "Literal 0x...:             ${literal_avg} µs (avg)"
echo "Ratio (nested/literal):   ${ratio}x"
echo ""
if [ "$ratio" != "N/A" ]; then
	ratio_int="${ratio%%.*}"
	[ -z "$ratio_int" ] && ratio_int="0"
	if [ "$ratio_int" -gt 2 ] 2>/dev/null; then
		echo "NOTE: Nested is ${ratio}x slower. Fix should bring ratio close to 1.0x"
	fi
fi
