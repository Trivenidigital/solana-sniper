"""Tests for wallet management."""

from sniper.wallet import generate_keypair, load_keypair


def test_generate_keypair(tmp_path):
    path = tmp_path / "wallet.json"
    kp = generate_keypair(path)
    assert path.exists()
    assert len(bytes(kp)) == 64


def test_load_keypair_existing(tmp_path):
    path = tmp_path / "wallet.json"
    kp1 = generate_keypair(path)
    kp2 = load_keypair(path)
    assert str(kp1.pubkey()) == str(kp2.pubkey())


def test_load_keypair_generates_if_missing(tmp_path):
    path = tmp_path / "new_wallet.json"
    kp = load_keypair(path)
    assert path.exists()
    assert len(bytes(kp)) == 64
