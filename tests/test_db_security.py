import pytest

from database.db import Database
from security.vault import Pairing, Vault, hash_token, redact


def test_duplicate_signal_is_rejected(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    aid = db.add_account(123, "Broker-Demo", "x", True, None, "ref")
    k = Database.new_key()
    assert db.begin_order(k, aid, "XAUUSD", "buy", "0.10") is True
    assert db.begin_order(k, aid, "XAUUSD", "buy", "0.10") is False  # same signal replayed


def test_unknown_order_stays_unresolved(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    aid = db.add_account(123, "S", None, True, None, "ref")
    k = Database.new_key()
    db.begin_order(k, aid, "XAUUSD", "buy", "0.1")
    db.set_order_state(k, "UNKNOWN", detail="connection lost mid-send")
    assert [o["idempotency_key"] for o in db.unresolved_orders(aid)] == [k]
    db.set_order_state(k, "FILLED", broker_position=99)
    assert db.unresolved_orders(aid) == []
    with pytest.raises(ValueError):
        db.set_order_state(k, "BOGUS")


def test_account_has_no_password_column(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    cols = [r[1] for r in db._conn.execute("PRAGMA table_info(accounts)")]
    assert "password" not in cols and "credential_ref" in cols


def test_vault_roundtrip_and_revoke(tmp_path):
    v = Vault(str(tmp_path / "v"))
    ref = v.store("hunter2")
    assert "hunter2" not in open(tmp_path / "v" / (ref + ".bin"), "rb").read().decode("latin1")
    assert v.load(ref) == "hunter2"
    v.revoke(ref)
    with pytest.raises(FileNotFoundError):
        v.load(ref)
    with pytest.raises(ValueError):
        v.load("../etc/passwd")


def test_pairing_single_use_and_rate_limit():
    p = Pairing(max_tries=3)
    code = p.new_code()
    assert p.redeem("000000" if code != "000000" else "111111") is None
    token = p.redeem(code)
    assert token and len(token) > 30
    assert p.redeem(code) is None  # single use
    code = p.new_code()
    for _ in range(3):
        p.redeem("bad")
    assert p.redeem(code) is None  # locked out after too many tries


def test_device_token_revocation(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    h = hash_token("tok")
    db.add_device("phone", h)
    assert db.device_valid(h)
    db.revoke_device(h)
    assert not db.device_valid(h)


def test_redact():
    assert redact("login failed for pw=hunter2", ["hunter2"]) == "login failed for pw=***"
