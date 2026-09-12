#!/usr/bin/env bash
# Smoke test end-to-end (§15): gira in una arena temporanea, non tocca il repo.
#
#   ./scripts/test_smoke.sh
#
# Percorso: seed -> credit(3) -> bet(yes/yes/no) -> casi negativi
#           -> lotto via drain -> settle(yes) -> assert I3 -> rebuild I1/I2.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
PY="${PYTHON:-python3}"

ARENA="$(mktemp -d -t arena-smoke-XXXXXX)"
trap 'rm -rf "$ARENA"' EXIT
export ARENA_ROOT="$ARENA"
export ARENA_SECRETS_FILE="$ARENA/.secrets.json"
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '   \033[32mok\033[0m %s\n' "$*"; }
fail() { printf '   \033[31mFALLITO\033[0m %s\n' "$*" >&2; exit 1; }

# sign <user> <event> <side> <amount> <nonce> -> payload JSON firmato su stdout
sign() {
  "$PY" - "$@" <<'PYEOF'
import json, os, sys
import common as c
user, event, side, amount, nonce = sys.argv[1:6]
secrets = json.load(open(os.environ["ARENA_SECRETS_FILE"]))
bet = {"bet_id": f"{user}-{nonce}", "user_id": user, "event_id": event,
       "side": side, "amount": int(amount), "nonce": int(nonce), "ts": c.now_iso()}
bet["sig"] = c.sign_bet(secrets[user], bet)
print(json.dumps(bet))
PYEOF
}

step "seed: tre utenti"
"$PY" - <<'PYEOF'
import json, os, secrets
users = {u: secrets.token_hex(32) for u in ("u_mario", "u_luca", "u_anna")}
json.dump(users, open(os.environ["ARENA_SECRETS_FILE"], "w"), indent=2)
PYEOF
for u in u_mario u_luca u_anna; do
  "$PY" "$HERE/credit.py" --user "$u" --amount 10000 --id "seed-$u" >/dev/null
done
ok "3 utenti accreditati a 10000"

step "evento aperto"
CLOSE="$("$PY" -c 'import datetime as d; print((d.datetime.now(d.timezone.utc)+d.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))')"
"$PY" "$HERE/create_event.py" --id ev_smoke --title "Smoke test" \
  --close-at "$CLOSE" --takeout-bps 300 --min-bet 100 --max-bet 5000 >/dev/null
ok "ev_smoke OPEN fino a $CLOSE"

step "tre bet valide (yes/yes/no) via workflow_dispatch"
"$PY" "$HERE/place_bet.py" --json "$(sign u_mario ev_smoke yes 100 1)"
"$PY" "$HERE/place_bet.py" --json "$(sign u_luca  ev_smoke yes 200 1)"
"$PY" "$HERE/place_bet.py" --json "$(sign u_anna  ev_smoke no  100 1)"

step "casi negativi"
# Una bet rifiutata esce con 10 (esito normale), MAI con 1 (guasto).
expect_rejected() {  # expect_rejected <descrizione> <payload>
  set +e; "$PY" "$HERE/place_bet.py" --json "$2" >/dev/null; local rc=$?; set -e
  [ "$rc" -eq 10 ] || fail "$1: atteso exit 10 (REJECTED), ottenuto $rc"
  ok "$1 (exit 10)"
}
BAD="$(sign u_mario ev_smoke yes 100 2 | "$PY" -c 'import json,sys; b=json.load(sys.stdin); b["amount"]=5000; print(json.dumps(b))')"
expect_rejected "ERR_SIG" "$BAD"
"$PY" "$HERE/place_bet.py" --json "$(sign u_mario ev_smoke yes 100 1)" | grep -q DUP \
  && ok "DUP (exit 0: un duplicato e' un successo)" || fail "DUP atteso"
"$PY" "$HERE/place_bet.py" --json "$(sign u_anna ev_smoke no 4000 2)" >/dev/null
"$PY" "$HERE/place_bet.py" --json "$(sign u_anna ev_smoke no 4000 3)" >/dev/null
expect_rejected "ERR_BALANCE" "$(sign u_anna ev_smoke no 4000 4)"

step "lotto via coda di issue (drain)"
QUEUE="$ARENA/queue.json"
BET_LUCA="$(sign u_luca ev_smoke no 300 2)" \
"$PY" - "$QUEUE" <<'PYEOF'
import json, os, sys
bet = os.environ["BET_LUCA"]
issues = [
    {"number": 11, "body": "BET\n\n```json\n" + bet + "\n```\n"},
    {"number": 12, "body": "niente json qui, solo chiacchiere"},
]
json.dump(issues, open(sys.argv[1], "w"), indent=2)
PYEOF
"$PY" "$HERE/drain.py" --issues-file "$QUEUE" --effects-file "$ARENA/effects.json" \
  --skip-issue-updates
"$PY" - <<'PYEOF'
import json, os
root = os.environ["ARENA_ROOT"]
r = json.load(open(os.path.join(root, "receipts.json")))["receipts"]
assert r["u_luca-2"]["code"] == "OK_APPLIED", r["u_luca-2"]
PYEOF
ok "lotto drenato: 1 applicata, 1 rifiutata (ERR_MALFORMED)"

step "settlement: outcome = yes"
"$PY" "$HERE/settle.py" --event ev_smoke --outcome yes --force

step "assert di conformita'"
"$PY" - <<'PYEOF'
import json, os
root = os.environ["ARENA_ROOT"]
s = json.load(open(os.path.join(root, "settled.json")))["settlements"]["ev_smoke"]
b = json.load(open(os.path.join(root, "balances.json")))["balances"]

# I3 — conservazione
assert s["payout_total"] + s["takeout"] + s["dust_to_house"] == s["T"], s
assert s["payout_total"] + s["house_total"] == s["T"], s
assert s["payout_total"] <= s["distributable"], s
print(f"   I3 ok: T={s['T']} payout_total={s['payout_total']} "
      f"takeout={s['takeout']} dust={s['dust_to_house']} house={s['house_total']}")

# at_risk azzerato
risky = {u: r["at_risk"] for u, r in b.items() if r["at_risk"] != 0}
assert not risky, f"at_risk non azzerato: {risky}"
print("   at_risk ok: " + ", ".join(f"{u}={r['available']}" for u, r in sorted(b.items())))
PYEOF
ok "conservazione e at_risk"

step "I1 catena + I2 rebuild == proiezione"
"$PY" "$HERE/rebuild_balances.py"

printf '\n\033[32mSMOKE TEST OK\033[0m\n'
