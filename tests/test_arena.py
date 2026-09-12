#!/usr/bin/env python3
"""Suite di conformita' (§15): ogni test mappa su una invariante I1-I10.

Gira senza rete e senza git:

    python3 -m unittest discover -s tests -v
    python3 tests/test_arena.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import common as c  # noqa: E402
import drain as drain_mod  # noqa: E402
import settle as settle_mod  # noqa: E402

sys.path.insert(0, str(REPO / "client"))
import arena as client  # noqa: E402


def iso(delta_minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).strftime(
        c.ISO_FMT
    )


class ArenaCase(unittest.TestCase):
    """Base: un'arena vuota in una directory temporanea, per ogni test."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="arena-test-"))
        self.secrets_file = self.tmp / ".secrets.json"
        self._env_backup = dict(os.environ)
        os.environ["ARENA_ROOT"] = str(self.tmp)
        os.environ["ARENA_SECRETS_FILE"] = str(self.secrets_file)
        os.environ.pop("USER_SECRETS", None)
        os.environ.pop("AGE_KEY", None)
        self.secrets = {}
        self.write_secrets()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helper di setup ---------------------------------------------------
    def write_secrets(self):
        self.secrets_file.write_text(json.dumps(self.secrets, indent=2), encoding="utf-8")

    def add_user(self, user_id: str, secret: str = None) -> str:
        secret = secret or ("s" * 8 + user_id)
        self.secrets[user_id] = secret
        self.write_secrets()
        return secret

    def run_script(self, name: str, *args, expect=0):
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / name), *args],
            capture_output=True,
            text=True,
            env=os.environ.copy(),
            cwd=str(self.tmp),
        )
        if expect is not None:
            self.assertEqual(
                proc.returncode,
                expect,
                f"{name} {' '.join(args)} -> {proc.returncode}\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}",
            )
        return proc

    def credit(self, user_id: str, amount: int, credit_id: str = None):
        args = ["--user", user_id, "--amount", str(amount)]
        if credit_id:
            args += ["--id", credit_id]
        return self.run_script("credit.py", *args)

    def make_event(self, event_id="ev1", *, close_in=60, takeout_bps=300,
                   min_bet=1, max_bet=10 ** 9, void_if_one_sided=True, state="OPEN"):
        doc = c.load_json(c.events_path(), c.default_events())
        doc["events"][event_id] = {
            "id": event_id,
            "title": f"test {event_id}",
            "side_labels": {"yes": "Si", "no": "No"},
            "state": state,
            "open_at": iso(-60),
            "close_at": iso(close_in),
            "pool": {"yes": 0, "no": 0},
            "bet_count": {"yes": 0, "no": 0},
            "takeout_bps": takeout_bps,
            "min_bet": min_bet,
            "max_bet": max_bet,
            "outcome": None,
            "resolved_at": None,
            "settled_at": None,
            "void_if_one_sided": void_if_one_sided,
            "currency": "PTS",
            "unit_scale": 1,
        }
        c.save_json(c.events_path(), doc)
        return doc["events"][event_id]

    def make_bet(self, user_id, event_id, side, amount, nonce, *,
                 secret=None, ts=None, **overrides):
        bet = {
            "bet_id": f"{user_id}-{nonce}",
            "user_id": user_id,
            "event_id": event_id,
            "side": side,
            "amount": amount,
            "nonce": nonce,
            "ts": ts or iso(0),
        }
        bet["sig"] = c.sign_bet(secret or self.secrets[user_id], bet)
        bet.update(overrides)
        return bet

    def place(self, bet, expect=None):
        proc = self.run_script("place_bet.py", "--json", json.dumps(bet), expect=expect)
        return proc

    # -- helper di lettura -------------------------------------------------
    def balances(self) -> dict:
        return c.load_json(c.balances_path(), c.default_balances())["balances"]

    def receipts(self) -> dict:
        return c.load_json(c.receipts_path(), c.default_receipts())["receipts"]

    def events(self) -> dict:
        return c.load_json(c.events_path(), c.default_events())["events"]

    def settlements(self) -> dict:
        return c.load_json(c.settled_path(), c.default_settled())["settlements"]

    def ledger(self) -> list:
        return c.read_ledger(self.tmp / "ledger")

    def assert_healthy(self):
        """I1 + I2 + I6: catena integra, proiezione == ledger, indice coerente."""
        sys.path.insert(0, str(SCRIPTS))
        import rebuild_balances

        report = rebuild_balances.audit(self.tmp)
        self.assertEqual(report["chain"], [], "catena del ledger rotta (I1)")
        self.assertEqual(report["balances"], [], "proiezione != ledger (I2)")
        self.assertEqual(report["state"], [], "ledger/state.json incoerente (I6)")
        self.assertEqual(report["negative"], [], "saldo negativo (I4)")
        return report


# ==========================================================================
class TestCrypto(ArenaCase):
    """I10 — autenticita' della bet."""

    def test_canonical_string_is_fixed_order(self):
        bet = {
            "bet_id": "u_1-1", "user_id": "u_1", "event_id": "ev1", "side": "yes",
            "amount": 100, "nonce": 1, "ts": "2026-09-12T10:00:00Z",
        }
        self.assertEqual(
            c.bet_signing_string(bet),
            "v1|u_1-1|u_1|ev1|yes|100|1|2026-09-12T10:00:00Z",
        )

    def test_key_order_in_json_does_not_change_signature(self):
        secret = "deadbeef"
        a = {"bet_id": "u_1-1", "user_id": "u_1", "event_id": "ev1", "side": "no",
             "amount": 7, "nonce": 1, "ts": "2026-09-12T10:00:00Z"}
        b = dict(reversed(list(a.items())))
        self.assertEqual(c.sign_bet(secret, a), c.sign_bet(secret, b))

    def test_tampering_any_field_breaks_the_signature(self):
        secret = "deadbeef"
        bet = {"bet_id": "u_1-1", "user_id": "u_1", "event_id": "ev1", "side": "yes",
               "amount": 100, "nonce": 1, "ts": "2026-09-12T10:00:00Z"}
        bet["sig"] = c.sign_bet(secret, bet)
        self.assertTrue(c.verify_bet_sig(secret, bet))
        for field, value in (("amount", 999), ("side", "no"), ("user_id", "u_2"),
                             ("event_id", "ev2"), ("nonce", 2)):
            tampered = dict(bet, **{field: value})
            self.assertFalse(c.verify_bet_sig(secret, tampered), f"campo {field}")
        self.assertFalse(c.verify_bet_sig("altro-segreto", bet))


# ==========================================================================
class TestLedger(ArenaCase):
    """I1 + I2 — catena e reducer."""

    def test_chain_links_and_rebuild(self):
        self.add_user("u_a")
        ws = c.WorkingState()
        ws.append_entry(c.KIND_CREDIT, "cr1", "u_a", 1000)
        ws.balance("u_a")["available"] += 1000
        ws.balance("u_a")["lifetime_credited"] += 1000
        ws.append_entry(c.KIND_CREDIT, "cr2", "u_a", -250)
        ws.balance("u_a")["available"] -= 250
        ws.flush()

        entries = self.ledger()
        self.assertEqual([e["seq"] for e in entries], [1, 2])
        self.assertEqual(entries[0]["prev_hash"], c.GENESIS_HASH)
        self.assertEqual(entries[1]["prev_hash"], entries[0]["hash"])
        self.assertEqual(c.verify_chain(entries), [])

        rebuilt, last_seq, head = c.reduce_balances(entries)
        self.assertEqual(rebuilt["u_a"]["available"], 750)
        self.assertEqual(rebuilt["u_a"]["lifetime_credited"], 1000)
        self.assertEqual(last_seq, 2)
        self.assertEqual(head, entries[-1]["hash"])
        self.assert_healthy()

    def test_tampering_the_ledger_is_detected(self):
        self.add_user("u_a")
        self.credit("u_a", 1000)
        shard = c.ledger_shards(self.tmp / "ledger")[0]
        entry = json.loads(shard.read_text(encoding="utf-8").strip())
        entry["amount"] = 999999
        shard.write_text(c.canonical(entry) + "\n", encoding="utf-8")

        problems = c.verify_chain(self.ledger())
        self.assertTrue(any("hash non verifica" in p for p in problems), problems)
        proc = self.run_script("rebuild_balances.py", expect=1)
        self.assertIn("PROBLEMI", proc.stdout)

    def test_credit_is_idempotent_on_id(self):
        self.add_user("u_a")
        self.credit("u_a", 500, credit_id="cr_seed")
        self.credit("u_a", 500, credit_id="cr_seed")
        self.assertEqual(self.balances()["u_a"]["available"], 500)
        self.assertEqual(len(self.ledger()), 1)
        self.assert_healthy()

    def test_negative_credit_cannot_go_below_zero_without_force(self):
        self.add_user("u_a")
        self.credit("u_a", 100)
        self.run_script("credit.py", "--user", "u_a", "--amount", "-500", expect=1)
        self.assertEqual(self.balances()["u_a"]["available"], 100)
        self.run_script("credit.py", "--user", "u_a", "--amount", "-500",
                        "--allow-negative", expect=0)
        self.assertEqual(self.balances()["u_a"]["available"], -400)


# ==========================================================================
class TestValidationPipeline(ArenaCase):
    """§8.1 — ordine dei controlli e codici di rifiuto."""

    def setUp(self):
        super().setUp()
        self.add_user("u_a")
        self.credit("u_a", 10000)
        self.make_event("ev1", min_bet=100, max_bet=5000)

    def code_for(self, bet) -> str:
        ws = c.WorkingState()
        res = c.validate_and_apply(bet, ws, c.load_user_secrets())
        return res.code

    def test_happy_path(self):
        bet = self.make_bet("u_a", "ev1", "yes", 1000, 1)
        self.place(bet, expect=0)
        row = self.balances()["u_a"]
        self.assertEqual((row["available"], row["at_risk"], row["last_nonce"]),
                         (9000, 1000, 1))
        ev = self.events()["ev1"]
        self.assertEqual(ev["pool"], {"yes": 1000, "no": 0})
        self.assertEqual(ev["bet_count"], {"yes": 1, "no": 0})
        receipt = self.receipts()["u_a-1"]
        self.assertEqual((receipt["status"], receipt["code"]), ("APPLIED", "OK_APPLIED"))
        self.assertEqual(receipt["available"], 9000)
        self.assert_healthy()

    def test_malformed_missing_field(self):
        bet = self.make_bet("u_a", "ev1", "yes", 100, 1)
        bet.pop("nonce")
        self.assertEqual(self.code_for(bet), c.ERR_MALFORMED)

    def test_malformed_amount_not_integer(self):
        bet = {"bet_id": "u_a-1", "user_id": "u_a", "event_id": "ev1", "side": "yes",
               "amount": "100", "nonce": 1, "ts": iso(0)}
        bet["sig"] = c.sign_bet(self.secrets["u_a"], bet)
        self.assertEqual(self.code_for(bet), c.ERR_MALFORMED)

    def test_malformed_bet_id_not_derived_from_nonce(self):
        bet = self.make_bet("u_a", "ev1", "yes", 100, 1)
        bet["bet_id"] = "u_a-999"
        bet["sig"] = c.sign_bet(self.secrets["u_a"], bet)
        self.assertEqual(self.code_for(bet), c.ERR_MALFORMED)

    def test_unknown_user(self):
        bet = self.make_bet("u_ghost", "ev1", "yes", 100, 1, secret="qualsiasi")
        self.assertEqual(self.code_for(bet), c.ERR_UNKNOWN_USER)

    def test_bad_signature(self):
        bet = self.make_bet("u_a", "ev1", "yes", 100, 1)
        bet["amount"] = 5000  # firmata per 100
        self.assertEqual(self.code_for(bet), c.ERR_SIG)

    def test_cannot_bet_as_another_user(self):
        """Il rischio numero uno: l'innesco e' condiviso, il segreto no."""
        self.add_user("u_b")
        self.credit("u_b", 10000)
        forged = self.make_bet("u_b", "ev1", "yes", 1000, 1,
                               secret=self.secrets["u_a"])
        self.assertEqual(self.code_for(forged), c.ERR_SIG)
        self.assertEqual(self.balances()["u_b"]["available"], 10000)

    def test_event_missing(self):
        bet = self.make_bet("u_a", "ev_nope", "yes", 100, 1)
        self.assertEqual(self.code_for(bet), c.ERR_EVENT_MISSING)

    def test_event_window_closed(self):
        self.make_event("ev_past", close_in=-1)
        bet = self.make_bet("u_a", "ev_past", "yes", 100, 1)
        self.assertEqual(self.code_for(bet), c.ERR_EVENT_CLOSED)

    def test_event_already_settled(self):
        self.make_event("ev_done", state="SETTLED")
        bet = self.make_bet("u_a", "ev_done", "yes", 100, 1)
        self.assertEqual(self.code_for(bet), c.ERR_EVENT_CLOSED)

    def test_limits(self):
        self.assertEqual(self.code_for(self.make_bet("u_a", "ev1", "yes", 50, 1)),
                         c.ERR_LIMITS)
        self.assertEqual(self.code_for(self.make_bet("u_a", "ev1", "yes", 6000, 1)),
                         c.ERR_LIMITS)

    def test_nonce_must_increase(self):
        self.place(self.make_bet("u_a", "ev1", "yes", 100, 5), expect=0)
        # bet_id nuovo ma nonce vecchio: non e' un duplicato, e' un replay
        bet = self.make_bet("u_a", "ev1", "yes", 100, 3)
        self.place(bet, expect=c.EXIT_REJECTED)
        self.assertEqual(self.receipts()["u_a-3"]["code"], c.ERR_NONCE)
        self.assertEqual(self.receipts()["u_a-3"]["status"], "REJECTED")
        self.assertEqual(self.balances()["u_a"]["last_nonce"], 5)
        # anche riusare esattamente lo stesso nonce accettato e' un nonce basso
        self.assertEqual(self.code_for(self.make_bet("u_a", "ev1", "yes", 100, 5)),
                         c.DUP)

    def test_insufficient_balance(self):
        bet = self.make_bet("u_a", "ev1", "yes", 10001, 1)
        # sopra max_bet scatta prima il controllo limiti, quindi alzo il tetto
        self.make_event("ev_big", min_bet=1, max_bet=10 ** 9)
        bet = self.make_bet("u_a", "ev_big", "yes", 10001, 1)
        self.assertEqual(self.code_for(bet), c.ERR_BALANCE)
        self.assertEqual(self.balances()["u_a"]["available"], 10000)

    def test_duplicate_bet_is_a_noop_success(self):
        bet = self.make_bet("u_a", "ev1", "yes", 1000, 1)
        self.place(bet, expect=0)
        proc = self.place(bet, expect=0)
        self.assertIn(c.DUP, proc.stdout)
        self.assertEqual(self.balances()["u_a"]["available"], 9000)
        self.assertEqual(len(self.ledger()), 2)  # credit + 1 sola BET_DEBIT
        self.assertEqual(self.receipts()["u_a-1"]["status"], "APPLIED")
        self.assert_healthy()

    def test_replay_after_settle_is_still_a_noop(self):
        bet = self.make_bet("u_a", "ev1", "yes", 1000, 1)
        self.place(bet, expect=0)
        self.add_user("u_b")
        self.credit("u_b", 5000)
        self.place(self.make_bet("u_b", "ev1", "no", 1000, 1), expect=0)
        self.run_script("settle.py", "--event", "ev1", "--outcome", "yes", "--force")
        before = json.dumps(self.balances(), sort_keys=True)
        self.place(bet, expect=0)
        self.assertEqual(json.dumps(self.balances(), sort_keys=True), before)
        self.assert_healthy()


# ==========================================================================
class TestSettlement(ArenaCase):
    """§9 + I3 — conservazione, rimborsi, idempotenza."""

    def setUp(self):
        super().setUp()
        for user_id in ("u_a", "u_b", "u_c"):
            self.add_user(user_id)
            self.credit(user_id, 10000)

    def bet(self, user_id, event_id, side, amount, nonce):
        self.place(self.make_bet(user_id, event_id, side, amount, nonce), expect=0)

    def test_win_conserves_and_sends_dust_to_house(self):
        self.make_event("ev1", takeout_bps=300)
        self.bet("u_a", "ev1", "yes", 100, 1)
        self.bet("u_b", "ev1", "yes", 200, 1)
        self.bet("u_c", "ev1", "no", 100, 1)

        self.run_script("settle.py", "--event", "ev1", "--outcome", "yes", "--force")
        rec = self.settlements()["ev1"]

        self.assertEqual(rec["T"], 400)
        self.assertEqual(rec["takeout"], 12)            # 400*300//10000
        self.assertEqual(rec["distributable"], 388)
        self.assertEqual(rec["payout_total"], 387)      # 129 + 258
        self.assertEqual(rec["dust_to_house"], 1)
        self.assertEqual(rec["house_total"], 13)
        # I3: payout_total + takeout + dust == T
        self.assertEqual(rec["payout_total"] + rec["takeout"] + rec["dust_to_house"],
                         rec["T"])

        bal = self.balances()
        self.assertEqual(bal["u_a"]["available"], 10000 - 100 + 129)
        self.assertEqual(bal["u_b"]["available"], 10000 - 200 + 258)
        self.assertEqual(bal["u_c"]["available"], 10000 - 100)
        for user_id in ("u_a", "u_b", "u_c"):
            self.assertEqual(bal[user_id]["at_risk"], 0, user_id)

        ev = self.events()["ev1"]
        self.assertEqual((ev["state"], ev["outcome"]), ("SETTLED", "yes"))
        self.assertIsNotNone(ev["settled_at"])
        self.assert_healthy()

    def test_void_refunds_everything_without_takeout(self):
        self.make_event("ev1", takeout_bps=1000)
        self.bet("u_a", "ev1", "yes", 700, 1)
        self.bet("u_b", "ev1", "no", 300, 1)
        self.run_script("settle.py", "--event", "ev1", "--outcome", "void")

        rec = self.settlements()["ev1"]
        self.assertTrue(rec["void"])
        self.assertEqual((rec["takeout"], rec["house_total"]), (0, 0))
        self.assertEqual(rec["payout_total"], rec["T"])   # I3 rimborso
        bal = self.balances()
        self.assertEqual(bal["u_a"]["available"], 10000)
        self.assertEqual(bal["u_b"]["available"], 10000)
        self.assertEqual(bal["u_a"]["at_risk"] + bal["u_b"]["at_risk"], 0)
        self.assert_healthy()

    def test_one_sided_market_is_refunded(self):
        self.make_event("ev1", takeout_bps=500, void_if_one_sided=True)
        self.bet("u_a", "ev1", "yes", 500, 1)
        self.bet("u_b", "ev1", "yes", 500, 1)
        self.run_script("settle.py", "--event", "ev1", "--outcome", "yes", "--force")

        rec = self.settlements()["ev1"]
        self.assertTrue(rec["void"])
        self.assertEqual(rec["payout_total"], 1000)
        self.assertEqual(rec["takeout"], 0)
        bal = self.balances()
        self.assertEqual(bal["u_a"]["available"], 10000)
        self.assertEqual(bal["u_b"]["available"], 10000)
        self.assert_healthy()

    def test_one_sided_without_flag_pays_the_only_side(self):
        self.make_event("ev1", takeout_bps=500, void_if_one_sided=False)
        self.bet("u_a", "ev1", "yes", 500, 1)
        self.bet("u_b", "ev1", "yes", 500, 1)
        self.run_script("settle.py", "--event", "ev1", "--outcome", "yes", "--force")

        rec = self.settlements()["ev1"]
        self.assertFalse(rec["void"])
        self.assertEqual(rec["takeout"], 50)             # 1000*500//10000
        self.assertEqual(rec["payout_total"], 950)
        bal = self.balances()
        self.assertEqual(bal["u_a"]["available"], 10000 - 500 + 475)
        self.assert_healthy()

    def test_losing_side_wins_nothing_but_frees_at_risk(self):
        self.make_event("ev1", takeout_bps=0)
        self.bet("u_a", "ev1", "yes", 1000, 1)
        self.bet("u_b", "ev1", "no", 1000, 1)
        self.run_script("settle.py", "--event", "ev1", "--outcome", "no", "--force")
        bal = self.balances()
        self.assertEqual(bal["u_a"]["available"], 9000)
        self.assertEqual(bal["u_a"]["at_risk"], 0)
        self.assertEqual(bal["u_b"]["available"], 11000)
        self.assert_healthy()

    def test_outcome_with_empty_winning_pool_is_refunded(self):
        self.make_event("ev1", takeout_bps=300, void_if_one_sided=False)
        self.bet("u_a", "ev1", "no", 400, 1)
        self.bet("u_b", "ev1", "no", 600, 1)
        self.run_script("settle.py", "--event", "ev1", "--outcome", "yes", "--force")
        rec = self.settlements()["ev1"]
        self.assertTrue(rec["void"])
        self.assertEqual(self.balances()["u_a"]["available"], 10000)
        self.assert_healthy()

    def test_event_without_bets_settles_clean(self):
        self.make_event("ev_empty", close_in=-1)
        self.run_script("settle.py", "--event", "ev_empty", "--outcome", "yes")
        rec = self.settlements()["ev_empty"]
        self.assertEqual((rec["T"], rec["payout_total"], rec["house_total"]), (0, 0, 0))
        self.assertEqual(self.events()["ev_empty"]["state"], "SETTLED")
        self.assert_healthy()

    def test_settle_is_idempotent(self):
        self.make_event("ev1")
        self.bet("u_a", "ev1", "yes", 1000, 1)
        self.bet("u_b", "ev1", "no", 1000, 1)
        self.run_script("settle.py", "--event", "ev1", "--outcome", "yes", "--force")
        snapshot = json.dumps(self.balances(), sort_keys=True)
        entries = len(self.ledger())

        proc = self.run_script("settle.py", "--event", "ev1", "--outcome", "yes",
                               "--force", expect=0)
        self.assertIn("DUP", proc.stdout)
        self.assertEqual(json.dumps(self.balances(), sort_keys=True), snapshot)
        self.assertEqual(len(self.ledger()), entries)
        self.assert_healthy()

    def test_cannot_settle_before_close_without_force(self):
        self.make_event("ev1", close_in=120)
        self.bet("u_a", "ev1", "yes", 100, 1)
        proc = self.run_script("settle.py", "--event", "ev1", "--outcome", "yes",
                               expect=1)
        self.assertIn("chiude alle", proc.stderr)
        self.assertEqual(self.settlements(), {})
        # ...ma il void (rimborso) e' sempre ammesso
        self.run_script("settle.py", "--event", "ev1", "--outcome", "void", expect=0)

    def test_settlement_uses_the_ledger_not_the_cached_pool(self):
        """I8: se la cache mente, vince il ledger."""
        self.make_event("ev1", takeout_bps=0)
        self.bet("u_a", "ev1", "yes", 1000, 1)
        self.bet("u_b", "ev1", "no", 1000, 1)
        doc = c.load_json(c.events_path(), c.default_events())
        doc["events"]["ev1"]["pool"] = {"yes": 999999, "no": 1}
        c.save_json(c.events_path(), doc)

        self.run_script("settle.py", "--event", "ev1", "--outcome", "yes", "--force")
        rec = self.settlements()["ev1"]
        self.assertEqual(rec["pool"], {"yes": 1000, "no": 1000})
        self.assertEqual(rec["T"], 2000)
        self.assertEqual(self.balances()["u_a"]["available"], 11000)
        self.assert_healthy()

    def test_compute_settlement_aborts_on_broken_conservation(self):
        """Gli assert di I3 devono scattare prima di qualunque scrittura."""
        stakes = {"yes": {"u_a": 100}, "no": {"u_b": 100}}
        with self.assertRaises(settle_mod.Abort):
            settle_mod.compute_settlement(stakes, {"yes": 100, "no": 100}, "yes",
                                          -100, False)


# ==========================================================================
class TestDrain(ArenaCase):
    """§7 — coda di issue, lotti, consegna at-least-once."""

    def setUp(self):
        super().setUp()
        for user_id in ("u_a", "u_b"):
            self.add_user(user_id)
            self.credit(user_id, 5000)
        self.make_event("ev1", min_bet=100, max_bet=4000)
        self.queue_path = self.tmp / "queue.json"
        self.effects_path = self.tmp / "effects.json"

    def queue(self, issues):
        self.queue_path.write_text(json.dumps(issues, indent=2), encoding="utf-8")

    def issue(self, number, bet):
        body = (f"BET {bet.get('bet_id')}\n\n```json\n"
                f"{json.dumps(bet, indent=2)}\n```\n")
        return {"number": number, "body": body, "state": "open"}

    def run_drain(self, expect=0):
        return self.run_script(
            "drain.py",
            "--issues-file", str(self.queue_path),
            "--effects-file", str(self.effects_path),
            expect=expect,
        )

    def effects(self):
        return json.loads(self.effects_path.read_text(encoding="utf-8"))

    def test_batch_sees_running_balance_within_the_same_run(self):
        """Due bet dello stesso utente nello stesso lotto: saldo e nonce vivi."""
        self.queue([
            self.issue(1, self.make_bet("u_a", "ev1", "yes", 3000, 1)),
            self.issue(2, self.make_bet("u_a", "ev1", "yes", 3000, 2)),  # sfora
            self.issue(3, self.make_bet("u_a", "ev1", "no", 2000, 3)),
        ])
        self.run_drain()
        receipts = self.receipts()
        self.assertEqual(receipts["u_a-1"]["code"], c.OK_APPLIED)
        self.assertEqual(receipts["u_a-2"]["code"], c.ERR_BALANCE)
        self.assertEqual(receipts["u_a-3"]["code"], c.OK_APPLIED)
        row = self.balances()["u_a"]
        self.assertEqual((row["available"], row["at_risk"], row["last_nonce"]),
                         (0, 5000, 3))
        self.assert_healthy()

    def test_issues_are_processed_in_arrival_order(self):
        self.queue([
            self.issue(7, self.make_bet("u_b", "ev1", "yes", 100, 2)),
            self.issue(3, self.make_bet("u_b", "ev1", "yes", 100, 1)),
        ])
        self.run_drain()
        self.assertEqual(self.receipts()["u_b-1"]["code"], c.OK_APPLIED)
        self.assertEqual(self.receipts()["u_b-2"]["code"], c.OK_APPLIED)
        self.assertEqual([e["op"] for e in self.effects()],
                         ["comment", "close", "comment", "close"])
        self.assertEqual(self.effects()[0]["number"], 3)

    def test_redrain_after_crash_is_a_noop(self):
        """Crash dopo il push, prima di chiudere le issue (§7.4)."""
        bets = [self.make_bet("u_a", "ev1", "yes", 1000, 1),
                self.make_bet("u_b", "ev1", "no", 500, 1)]
        self.queue([self.issue(1, bets[0]), self.issue(2, bets[1])])
        self.run_script("drain.py", "--issues-file", str(self.queue_path),
                        "--skip-issue-updates")
        snapshot = json.dumps(self.balances(), sort_keys=True)
        entries = len(self.ledger())

        self.run_drain()  # le issue sono ancora aperte: si ri-processa
        self.assertEqual(json.dumps(self.balances(), sort_keys=True), snapshot)
        self.assertEqual(len(self.ledger()), entries)
        for bet in bets:
            self.assertEqual(self.receipts()[bet["bet_id"]]["code"], c.DUP)
        self.assertEqual([e["op"] for e in self.effects()],
                         ["comment", "close", "comment", "close"])
        self.assertTrue(all(e.get("label") == "applied"
                            for e in self.effects() if e["op"] == "close"))
        self.assert_healthy()

    def test_malformed_issue_is_rejected_without_touching_the_ledger(self):
        self.queue([
            {"number": 1, "body": "ciao, vorrei scommettere 50 euro", "state": "open"},
            {"number": 2, "body": "```json\n{non json}\n```", "state": "open"},
        ])
        self.run_drain()
        self.assertEqual(self.ledger()[-1]["kind"], c.KIND_CREDIT)  # solo i seed
        closes = [e for e in self.effects() if e["op"] == "close"]
        self.assertEqual([e["label"] for e in closes], ["rejected", "rejected"])
        self.assert_healthy()

    def test_empty_queue_is_a_fast_noop(self):
        self.queue([])
        proc = self.run_drain()
        self.assertIn("coda vuota", proc.stdout)

    def test_duplicate_issues_in_the_same_batch(self):
        bet = self.make_bet("u_a", "ev1", "yes", 1000, 1)
        self.queue([self.issue(1, bet), self.issue(2, bet)])
        self.run_drain()
        debits = [e for e in self.ledger() if e["kind"] == c.KIND_BET_DEBIT]
        self.assertEqual(len(debits), 1)
        self.assertEqual(self.balances()["u_a"]["available"], 4000)

    def test_extract_json_block_variants(self):
        payload = {"a": 1}
        for body in (
            f"```json\n{json.dumps(payload)}\n```",
            f"testo\n\n```\n{json.dumps(payload)}\n```\n\ncoda",
            f"```JSON\n{json.dumps(payload)}\n```",
        ):
            self.assertEqual(drain_mod.parse_issue_body(body), payload)


# ==========================================================================
class TestOwnerTools(ArenaCase):
    """create_event / register / rebuild dal lato owner."""

    def test_create_event_cli(self):
        self.run_script(
            "create_event.py", "--id", "ev_derby", "--title", "Roma-Lazio",
            "--close-at", iso(90), "--yes", "Roma", "--no", "Lazio",
            "--takeout-bps", "250", "--min-bet", "50", "--max-bet", "900",
        )
        ev = self.events()["ev_derby"]
        self.assertEqual(ev["state"], "OPEN")
        self.assertEqual(ev["side_labels"], {"yes": "Roma", "no": "Lazio"})
        self.assertEqual((ev["takeout_bps"], ev["min_bet"], ev["max_bet"]),
                         (250, 50, 900))
        self.run_script("create_event.py", "--id", "ev_derby", "--title", "x",
                        "--close-at", iso(90), expect=1)

    def test_create_event_rejects_bad_window(self):
        self.run_script("create_event.py", "--id", "ev_x", "--title", "x",
                        "--close-at", iso(-10), expect=2)

    def test_register_creates_user_and_secret(self):
        proc = self.run_script("register.py", "--repo", "owner/arena", "--print-config")
        secrets_map = json.loads(self.secrets_file.read_text(encoding="utf-8"))
        self.assertEqual(len(secrets_map), 1)
        user_id, secret = next(iter(secrets_map.items()))
        self.assertRegex(user_id, r"^u_[0-9a-f]{8}$")
        self.assertEqual(len(secret), 64)
        self.assertIn(user_id, proc.stdout)
        self.assertIn(user_id, self.balances())
        self.assertEqual(self.balances()[user_id], c.zero_balance())
        self.assert_healthy()   # una riga a zero non e' un disallineamento

        # e l'utente appena registrato puo' subito scommettere
        self.secrets = secrets_map
        self.credit(user_id, 1000)
        self.make_event("ev1")
        self.place(self.make_bet(user_id, "ev1", "yes", 100, 1), expect=0)

    def test_register_refuses_without_a_delivery_channel(self):
        self.run_script("register.py", "--repo", "owner/arena", expect=2)

    def test_rebuild_repairs_a_corrupted_projection(self):
        self.add_user("u_a")
        self.credit("u_a", 1000)
        doc = c.load_json(c.balances_path(), c.default_balances())
        doc["balances"]["u_a"]["available"] = 999999
        c.save_json(c.balances_path(), doc)

        self.run_script("rebuild_balances.py", expect=1)
        self.run_script("rebuild_balances.py", "--write", expect=0)
        self.assertEqual(self.balances()["u_a"]["available"], 1000)
        self.run_script("rebuild_balances.py", expect=0)


# ==========================================================================
class TestOdds(ArenaCase):
    """§12.3 — quote implicite (unico punto con i float)."""

    def test_multiplier(self):
        pool = {"yes": 250, "no": 750}
        self.assertAlmostEqual(c.implied_multiplier(pool, "yes", 300),
                               0.97 * 1000 / 250)
        self.assertAlmostEqual(c.implied_multiplier(pool, "no", 300),
                               0.97 * 1000 / 750)
        self.assertIsNone(c.implied_multiplier({"yes": 0, "no": 10}, "yes", 300))
        self.assertIsNone(c.implied_multiplier({"yes": 0, "no": 0}, "no", 0))


# ==========================================================================
class TestClient(ArenaCase):
    """Il client e' un'altra implementazione della firma: deve combaciare."""

    def setUp(self):
        super().setUp()
        self.secret = self.add_user("u_ab12ef34")
        self.credit("u_ab12ef34", 5000)
        self.make_event("ev1", min_bet=100, max_bet=4000)
        self.cfg_dir = self.tmp / "cfg"
        self.cfg_dir.mkdir()
        self.cfg_path = self.cfg_dir / "config.json"
        self.cfg_path.write_text(json.dumps({
            "user_id": "u_ab12ef34", "secret": self.secret,
            "repo": "owner/arena", "branch": "main", "read": "raw",
            "channel": "issue",
        }), encoding="utf-8")
        os.environ["ARENA_CONFIG"] = str(self.cfg_path)
        os.environ["ARENA_LOCAL_ROOT"] = str(self.tmp)

    def test_signing_string_matches_the_server(self):
        bet = {"bet_id": "u_ab12ef34-3", "user_id": "u_ab12ef34", "event_id": "ev1",
               "side": "no", "amount": 250, "nonce": 3, "ts": "2026-09-12T09:00:00Z"}
        self.assertEqual(client.signing_string(bet), c.bet_signing_string(bet))
        self.assertEqual(client.sign_bet(self.secret, bet), c.sign_bet(self.secret, bet))

    def test_client_signed_bet_is_accepted_by_the_server(self):
        proc = subprocess.run(
            [sys.executable, str(REPO / "client" / "arena.py"), "sign", "ev1", "yes", "500"],
            capture_output=True, text=True, env=os.environ.copy(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        bet = json.loads(proc.stdout)
        self.assertEqual(bet["bet_id"], "u_ab12ef34-1")
        self.place(bet, expect=0)
        self.assertEqual(self.balances()["u_ab12ef34"]["available"], 4500)
        self.assert_healthy()

    def test_local_nonce_survives_a_pending_bet(self):
        """Fra invio e drain il last_nonce remoto non e' avanzato."""
        cfg = client.load_config()
        first = client.next_nonce(cfg, 0)
        client.remember_nonce(cfg, first)
        self.assertEqual(client.next_nonce(cfg, 0), first + 1)
        # quando il remoto recupera, e' lui a comandare
        self.assertEqual(client.next_nonce(cfg, 9), 10)

    def test_events_and_balance_views_read_the_repo_state(self):
        for argv in (["events"], ["balance"]):
            proc = subprocess.run(
                [sys.executable, str(REPO / "client" / "arena.py"), *argv],
                capture_output=True, text=True, env=os.environ.copy(),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
        proc = subprocess.run(
            [sys.executable, str(REPO / "client" / "arena.py"), "balance"],
            capture_output=True, text=True, env=os.environ.copy(),
        )
        self.assertIn("available:  5000", proc.stdout)

    def test_init_scrive_un_config_valido(self):
        nuovo = self.tmp / "nuovo" / "config.json"
        os.environ["ARENA_CONFIG"] = str(nuovo)
        proc = subprocess.run(
            [sys.executable, str(REPO / "client" / "arena.py"), "init",
             "--user-id", "u_ab12ef34", "--secret", "a" * 64,
             "--repo", "owner/arena", "--token", ""],
            capture_output=True, text=True, env=os.environ.copy(),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        cfg = json.loads(nuovo.read_text(encoding="utf-8"))
        self.assertEqual(cfg["user_id"], "u_ab12ef34")
        self.assertEqual(cfg["repo"], "owner/arena")
        self.assertNotIn("token", cfg, "un token vuoto non va salvato")
        self.assertEqual(oct(nuovo.stat().st_mode)[-3:], "600")

    def test_init_rifiuta_un_secret_malformato(self):
        nuovo = self.tmp / "nuovo2" / "config.json"
        os.environ["ARENA_CONFIG"] = str(nuovo)
        proc = subprocess.run(
            [sys.executable, str(REPO / "client" / "arena.py"), "init",
             "--user-id", "u_ab12ef34", "--secret", "non-esadecimale",
             "--repo", "owner/arena", "--token", ""],
            capture_output=True, text=True, env=os.environ.copy(),
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(nuovo.exists(), "un config invalido non va scritto")

    def test_init_non_sovrascrive_senza_force(self):
        proc = subprocess.run(
            [sys.executable, str(REPO / "client" / "arena.py"), "init",
             "--user-id", "u_ab12ef34", "--secret", "b" * 64,
             "--repo", "owner/arena", "--token", ""],
            capture_output=True, text=True, env=os.environ.copy(),
        )
        self.assertNotEqual(proc.returncode, 0)
        # il config di setUp e' intatto
        self.assertEqual(json.loads(self.cfg_path.read_text())["secret"], self.secret)

    def test_multiplier_matches_the_server_formula(self):
        pool = {"yes": 300, "no": 700}
        self.assertAlmostEqual(client.multiplier(pool, "yes", 300),
                               c.implied_multiplier(pool, "yes", 300))


# ==========================================================================
class TestConcurrency(unittest.TestCase):
    """Profilo minimo dello stress test (§stress): gira in CI a ogni push.

    I profili grandi si lanciano a mano:
        python3 scripts/stress_test.py --users 50 --bets 6 --runners 8
    """

    def test_concurrent_writers_preserve_every_invariant(self):
        workdir = Path(tempfile.mkdtemp(prefix="arena-ci-stress-"))
        try:
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "stress_test.py"),
                 "--users", "5", "--bets", "2", "--runners", "3",
                 "--rounds", "5", "--crash-rate", "0.25",
                 "--workdir", str(workdir)],
                capture_output=True, text=True, timeout=600,
            )
            self.assertEqual(
                proc.returncode, 0,
                f"lo stress test ha trovato violazioni:\n{proc.stdout}\n{proc.stderr}",
            )
            self.assertIn("NESSUNA VIOLAZIONE", proc.stdout)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)


# ==========================================================================
class TestHumanTypedInput(ArenaCase):
    """Gli identificatori digitati a mano arrivano sporchi (§test reale).

    Da telefono la tastiera aggiunge uno spazio in fondo con facilita': e'
    successo davvero, su un settle reale, ed e' costato un run rosso.
    """

    def setUp(self):
        super().setUp()
        self.add_user("u_a")
        self.credit("u_a", 5000)
        self.make_event("ev1", close_in=-1)

    def test_settle_tollera_spazi_nell_event_id(self):
        self.run_script("settle.py", "--event", " ev1 ", "--outcome", "yes")
        self.assertIn("ev1", self.settlements())
        self.assert_healthy()

    def test_credit_tollera_spazi_nello_user_id(self):
        self.credit(" u_a ", 100)
        # niente riga fantasma con lo spazio dentro
        self.assertEqual(sorted(self.balances()), ["u_a"])
        self.assertEqual(self.balances()["u_a"]["available"], 5100)
        self.assert_healthy()

    def test_create_event_tollera_spazi(self):
        self.run_script("create_event.py", "--id", " ev_x ", "--title", "x",
                        "--close-at", " " + iso(60) + " ")
        self.assertIn("ev_x", self.events())

    def test_il_payload_firmato_NON_viene_normalizzato(self):
        """La linea da non superare: l'HMAC copre la stringa esatta.

        Normalizzare dopo la verifica vorrebbe dire applicare una bet diversa
        da quella che l'utente ha firmato.
        """
        self.make_event("ev2")
        bet = self.make_bet("u_a", "ev2 ", "yes", 100, 1)   # firmata con lo spazio
        ws = c.WorkingState()
        res = c.validate_and_apply(bet, ws, c.load_user_secrets())
        self.assertEqual(res.code, c.ERR_EVENT_MISSING)
        self.assertEqual(ws.events["ev2"]["pool"], {"yes": 0, "no": 0})


class TestWorkflowInputs(unittest.TestCase):
    """I toggle dei workflow devono reggere sia "true" sia il booleano JSON.

    `github.event.inputs.x == 'true'` e' SEMPRE falso quando il client manda un
    booleano vero: GitHub converte i tipi a numero (true -> 1, 'true' -> NaN).
    L'app mobile manda booleani, quindi `dry_run` acceso liquidava davvero.
    """

    def workflows(self):
        return sorted((REPO / ".github" / "workflows").glob("*.yml"))

    def test_nessun_confronto_fragile_sui_booleani(self):
        offenders = []
        for path in self.workflows():
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if "${{" in line and "== 'true'" in line:
                    offenders.append(f"{path.name}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "confronto fragile su input booleano")

    def test_i_toggle_passano_da_variabili_d_ambiente(self):
        settle = (REPO / ".github/workflows/settle.yml").read_text()
        self.assertIn('IN_DRY_RUN: ${{ github.event.inputs.dry_run }}', settle)
        # fail-safe: si liquida solo se l'anteprima e' esplicitamente spenta
        self.assertIn('if [ "$IN_DRY_RUN" != "false" ]; then', settle)
        self.assertIn("--dry-run", settle)

    def test_nessun_workflow_maschera_gli_errori(self):
        for path in self.workflows():
            text = path.read_text()
            self.assertNotIn("|| true", text.replace("`|| true`", ""),
                             f"{path.name} maschera gli errori")


# ==========================================================================
class TestFixtureProvaReale(unittest.TestCase):
    """Rigioca il ledger della prima prova reale su GitHub Actions.

    E' l'unico test della suite i cui dati non sono inventati: li hanno
    prodotti i workflow su runner veri (vedi tests/fixtures/prova-reale/).
    Se una modifica futura cambia il significato di una entry, o la
    matematica del settlement, qui si vede subito.
    """

    FIXTURE = REPO / "tests" / "fixtures" / "prova-reale"

    def setUp(self):
        self.entries = c.read_ledger(self.FIXTURE / "ledger")
        self.balances = c.load_json(self.FIXTURE / "balances.json")["balances"]
        self.settled = c.load_json(self.FIXTURE / "settled.json")["settlements"]
        self.receipts = c.load_json(self.FIXTURE / "receipts.json")["receipts"]

    def test_la_catena_regge(self):
        self.assertEqual(c.verify_chain(self.entries), [])
        self.assertEqual(
            self.entries[-1]["hash"],
            "5aaf1f8131b978eac6c521a662181df381730f98a666f69610af550a20fd2b8f",
            "l'hash di testa e' cambiato: il formato delle entry non e' piu' "
            "quello con cui e' stato firmato questo ledger",
        )

    def test_i_saldi_si_ricostruiscono_uguali(self):
        rebuilt, last_seq, head = c.reduce_balances(self.entries)
        self.assertEqual(c.diff_balances(self.balances, rebuilt), [])
        self.assertEqual(last_seq, 6)

    def test_i_numeri_del_settlement(self):
        rec = self.settled["ev_prova"]
        self.assertEqual(rec["T"], 5000)
        self.assertEqual(rec["takeout"], 150)          # 5000 * 300 // 10000
        self.assertEqual(rec["distributable"], 4850)
        self.assertEqual(rec["winning_side"], "no")
        self.assertEqual(rec["winning_pool"], 2000)
        self.assertEqual(rec["payout_total"], 4850)
        self.assertEqual(rec["dust_to_house"], 0)
        self.assertEqual(rec["house_total"], 150)
        # I3: la conservazione, sui numeri veri
        self.assertEqual(rec["payout_total"] + rec["house_total"], rec["T"])

    def test_una_sola_settle_user_per_utente(self):
        """La seconda liquidazione fu un no-op: deve restare tale."""
        settles = [e for e in self.entries if e["kind"] == c.KIND_SETTLE_USER]
        self.assertEqual(len(settles), 2)
        self.assertEqual(len({e["user_id"] for e in settles}), 2)
        # anche il perdente ha la sua entry, altrimenti at_risk non torna a zero
        perdente = next(e for e in settles if e["amount"] == 0)
        self.assertEqual(perdente["meta"]["stake"], 3000)

    def test_conservazione_globale(self):
        credited = sum(e["amount"] for e in self.entries if e["kind"] == c.KIND_CREDIT)
        held = sum(r["available"] + r["at_risk"] for r in self.balances.values())
        house = sum(s["house_total"] for s in self.settled.values())
        self.assertEqual(credited, 20000)
        self.assertEqual(held + house, credited)
        self.assertTrue(all(r["at_risk"] == 0 for r in self.balances.values()))

    def test_le_bet_ostili_non_sono_nel_ledger(self):
        applicate = {e["id"] for e in self.entries if e["kind"] == c.KIND_BET_DEBIT}
        self.assertEqual(applicate, {"u_03b325ba-1", "u_1b676e26-1"})
        # la firma forgiata e la sonda hanno una ricevuta, ma nessuna entry
        self.assertEqual(self.receipts["u_1b676e26-2"]["code"], c.ERR_SIG)
        self.assertEqual(self.receipts["u_03b325ba-99"]["code"], c.ERR_EVENT_MISSING)
        for bet_id in ("u_1b676e26-2", "u_03b325ba-99"):
            self.assertNotIn(bet_id, applicate)

    def test_la_consegna_doppia_risulta_applicata_una_volta_sola(self):
        """La copia identica della issue #5: DUP, e una sola BET_DEBIT."""
        self.assertEqual(self.receipts["u_03b325ba-1"]["code"], c.DUP)
        self.assertEqual(self.receipts["u_03b325ba-1"]["status"], "APPLIED")
        debiti = [e for e in self.entries
                  if e["kind"] == c.KIND_BET_DEBIT and e["id"] == "u_03b325ba-1"]
        self.assertEqual(len(debiti), 1)
        self.assertEqual(debiti[0]["amount"], 3000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
