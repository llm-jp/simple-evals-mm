#!/usr/bin/env bash
# Release check (ABCI / a local machine with the cached data; not CI). Runs the real runner, store, verifiers and
# `sem run --from` on EVERY registered dataset with the scripted test-only provider `fake/` (samplers/fake.py: no
# network, no paid call), then the datasets.lock check and the live dataset smoke tests of the leaderboard datasets.
#
#   bash scripts/release_check.sh                   # from a clean, committed checkout with data/ (or its symlink)
#   nice -n 19 bash scripts/release_check.sh        # on a login node
#
# Steps (each PASS / FAIL / SKIP in the summary; exit 1 on any FAIL):
#   0. uv sync --frozen --reinstall-package simple-evals-mm (the editable install at HEAD)
#   1. sem run -m fake/scripted -d <all datasets> -l N -k 1 --generate-only, killed partway (SIGKILL)
#   2. sem job resume (the torn last line is cut, the missing trials generated), sem job show (every trial written)
#   3. sem job regrade --judge-model fake/judge, sem job show (every dataset verified), sem job check
#      (humaneval / humaneval_plus / livecodebench get allow_exec when bwrap exists, omnidocbench needs
#      OMNIDOCBENCH_EVAL_SIF and apptainer; otherwise they are generated but not verified, and listed)
#   4. sem run --from <job> on the same commit (--generate-only): 0 RERUN expected
#   5. sem datasets lock --check
#   6. pytest -m network -k <leaderboard datasets>
# Environment: RELEASE_CHECK_DIR (jobs dir, default a new temporary dir), RELEASE_CHECK_TASKS (-l, default 3),
#   RELEASE_CHECK_SKIP (comma-separated datasets left out, e.g. a dataset whose data is not cached here),
#   LEADERBOARD_DATASETS (comma-separated, default the v0.5 leaderboard set), SEM (default `uv run --frozen sem`),
#   RELEASE_CHECK_ALLOW_DIRTY=1 (only while editing this script: passes --allow-dirty).
set -u
cd "$(dirname "$0")/.."
export SEM_FAKE_PROVIDER=1
SEM="${SEM:-uv run --frozen sem}"
DIR="${RELEASE_CHECK_DIR:-$(mktemp -d -t sem-release-check.XXXXXX)}"
N_TASKS="${RELEASE_CHECK_TASKS:-3}"
LEADERBOARD="${LEADERBOARD_DATASETS:-hakushobench,heronbench,javlmbench,jdocqa,jgraphqa,jamultiimage,ccocrjavqa,cvqaja,charxiv,businessslidevqa,mathvision,aime26,countbenchqa,gpqa,mmmu,realworldqa,ifbench,mmmu_pro,zerobench,babyvision}"
JOB=rc_fake
JOB2=rc_fake_from
LOG="$DIR/logs"
mkdir -p "$LOG"
RESULTS=()
FAILED=0
step() {  # step NAME STATUS [NOTE]
  RESULTS+=("$(printf '%-34s %-5s %s' "$1" "$2" "${3:-}")")
  [ "$2" = FAIL ] && FAILED=1
  echo "[release-check] $1: $2 ${3:-}"
}
H="$DIR/helpers"  # the small JSON checks below (a file each: stdin carries the JSON)
mkdir -p "$H"
py() { local f="$1"; shift; uv run --frozen python "$H/$f.py" "$@"; }
cat > "$H/names.py" <<'EOF'
import json, sys
print(",".join(r["name"] for r in json.load(sys.stdin)))
EOF
cat > "$H/show.py" <<'EOF'
import json, sys
what, names = sys.argv[1], [n for n in sys.argv[2].split(",") if n]
s = json.load(sys.stdin)["summary"]
bad = []
for n in names:
    d = s.get(n)
    if d is None:
        bad.append(f"{n}: missing")
    elif what == "trials":
        bad += [f"{n} {a}: {t['done']}/{t['total']}" for a, t in d["trials"].items() if t["done"] != t["total"]]
    elif d["verification_id"] is None or not d["attempts"]:
        bad.append(f"{n}: not verified")
print("; ".join(bad))
sys.exit(1 if bad else 0)
EOF

echo "[release-check] jobs dir $DIR (logs in $LOG)"
# the editable install must be built at HEAD (`sem run` refuses a stale one: git worktrees, `uv run --no-sync`)
if uv sync --frozen --reinstall-package simple-evals-mm > "$LOG/uv_sync.log" 2>&1; then
  step "0 uv sync (install at HEAD)" PASS
else
  step "0 uv sync (install at HEAD)" FAIL "$LOG/uv_sync.log"
fi

# ---- the datasets
ALL=$($SEM datasets list --json > "$LOG/datasets.json" && py names < "$LOG/datasets.json")
if [ -z "$ALL" ]; then echo "[release-check] sem datasets list failed" >&2; exit 1; fi
SKIP=",${RELEASE_CHECK_SKIP:-},"
DATASETS=$(echo "$ALL" | tr ',' '\n' | while read -r d; do case "$SKIP" in *",$d,"*) ;; *) echo "$d";; esac; done | paste -sd,)
EXEC_ARGS=()
NOVERIFY=()
if command -v bwrap >/dev/null 2>&1; then
  for d in humaneval humaneval_plus livecodebench; do EXEC_ARGS+=(--dataset-arg "$d.allow_exec=true"); done
else
  NOVERIFY+=(humaneval humaneval_plus livecodebench)
fi
if [ -z "${OMNIDOCBENCH_EVAL_SIF:-}" ] || ! command -v apptainer >/dev/null 2>&1; then
  NOVERIFY+=(omnidocbench)
fi
VERIFY=$(echo "$DATASETS" | tr ',' '\n' | while read -r d; do case " ${NOVERIFY[*]:-} " in *" $d "*) ;; *) echo "$d";; esac; done | paste -sd,)
[ -n "${RELEASE_CHECK_SKIP:-}" ] && step "datasets left out" SKIP "$RELEASE_CHECK_SKIP"
[ ${#NOVERIFY[@]} -gt 0 ] && step "verification skipped" SKIP "${NOVERIFY[*]} (no bwrap / OMNIDOCBENCH_EVAL_SIF)"

DIRTY=()
[ "${RELEASE_CHECK_ALLOW_DIRTY:-}" = 1 ] && DIRTY=(--allow-dirty)  # developing this script only: a release runs clean
RUN_ARGS=("${DIRTY[@]}" -m fake/scripted -d "$DATASETS" -l "$N_TASKS" -k 1 -n 8 -o "$DIR" --judge-model fake/judge "${EXEC_ARGS[@]}")

# ---- 1. run, killed partway
NTOTAL=$(( $(echo "$DATASETS" | tr ',' '\n' | wc -l) * N_TASKS ))
SEM_FAKE_DELAY_S=0.3 $SEM run "${RUN_ARGS[@]}" --job-name "$JOB" --generate-only > "$LOG/run.log" 2>&1 &
PID=$!
KILLED=0
while kill -0 "$PID" 2>/dev/null; do
  n=$(cat "$DIR/$JOB"/*/trials_a1.jsonl 2>/dev/null | wc -l)
  if [ "$n" -ge $(( NTOTAL / 3 )) ]; then
    pkill -KILL -P "$PID" 2>/dev/null; kill -KILL "$PID" 2>/dev/null; KILLED=1; break
  fi
  sleep 1
done
wait "$PID" 2>/dev/null
RC=$?
if [ ! -f "$DIR/$JOB/config.json" ]; then
  step "1 run (generate-only)" FAIL "no job written (rc $RC; $LOG/run.log)"
elif [ "$KILLED" = 1 ]; then
  step "1 run (generate-only)" PASS "killed after $n/$NTOTAL trials"
else
  step "1 run (generate-only)" "$([ "$RC" = 0 ] && echo PASS || echo FAIL)" "finished before the kill (rc $RC; $LOG/run.log)"
fi

if [ ! -f "$DIR/$JOB/config.json" ]; then
  step "2-4 job steps" SKIP "no job to resume (step 1 failed)"
else
# ---- 2. resume + show
if $SEM job resume -p "$DIR/$JOB" -n 8 > "$LOG/resume.log" 2>&1; then
  step "2 job resume" PASS
else
  step "2 job resume" FAIL "$LOG/resume.log"
fi
check_show() {  # check_show WHAT: every dataset has all trials written (trials) / a verification (verified)
  $SEM job show "$DIR/$JOB" --json > "$LOG/show_$1.json" 2> "$LOG/show_$1.err" || { echo "sem job show failed"; return 1; }
  py show "$1" "$2" < "$LOG/show_$1.json"
}
if out=$(check_show trials "$DATASETS"); then step "2 job show (all trials)" PASS; else step "2 job show (all trials)" FAIL "$out"; fi

# ---- 3. regrade with the fake judge, show, check
if $SEM job regrade "$DIR/$JOB" -d "$VERIFY" --judge-model fake/judge "${DIRTY[@]}" > "$LOG/regrade.log" 2>&1; then
  step "3 job regrade (fake judge)" PASS
else
  step "3 job regrade (fake judge)" FAIL "$LOG/regrade.log"
fi
if out=$(check_show verified "$VERIFY"); then step "3 job show (all verified)" PASS; else step "3 job show (all verified)" FAIL "$out"; fi
if $SEM job check "$DIR/$JOB" > "$LOG/check.log" 2>&1; then step "3 job check" PASS; else step "3 job check" FAIL "$(tail -3 "$LOG/check.log" | paste -sd' ')"; fi

# ---- 4. derived job on the same commit: nothing to rerun
$SEM run "${RUN_ARGS[@]}" --job-name "$JOB2" --generate-only --from "$DIR/$JOB" > "$LOG/from.log" 2>&1
RC=$?
RERUN=$(grep -o 'rerun [0-9]*' "$LOG/from.log" | awk '{s += $2} END {print s + 0}')
NFROM=$(grep -c '^\[from\]' "$LOG/from.log")
if [ "$RC" = 0 ] && [ "$RERUN" = 0 ] && [ "$NFROM" -gt 0 ]; then
  step "4 run --from (same commit)" PASS "$NFROM datasets, 0 rerun"
else
  step "4 run --from (same commit)" FAIL "rc $RC, rerun $RERUN ($LOG/from.log)"
fi

fi

# ---- 5. datasets.lock
if $SEM datasets lock --check > "$LOG/lock_check.log" 2>&1; then
  step "5 datasets lock --check" PASS "$(tail -1 "$LOG/lock_check.log")"
else
  step "5 datasets lock --check" FAIL "$(tail -1 "$LOG/lock_check.log") ($LOG/lock_check.log)"
fi

# ---- 6. live smoke tests of the leaderboard datasets
# exact node ids (pytest -k matches substrings: mmmu would also select mmmu_pro and jmmmu)
mapfile -t NODES < <(echo "$LEADERBOARD" | tr ',' '\n' | sed 's/.*/tests\/test_dataset_registry.py::test_dataset_loads_and_verifies_one_task[&]/')
if uv run --frozen pytest -q -m network "${NODES[@]}" > "$LOG/pytest_network.log" 2>&1; then
  step "6 pytest -m network (leaderboard)" PASS "$(tail -1 "$LOG/pytest_network.log")"
else
  step "6 pytest -m network (leaderboard)" FAIL "$(tail -1 "$LOG/pytest_network.log") ($LOG/pytest_network.log)"
fi

echo
echo "==== release check summary ($DIR)"
printf '%s\n' "${RESULTS[@]}"
if [ "$FAILED" = 1 ]; then echo "RESULT: FAIL"; exit 1; fi
echo "RESULT: PASS"
