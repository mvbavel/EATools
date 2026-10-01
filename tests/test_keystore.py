"""Opt-in Keychain persistence of the Anthropic key, with `security` and Anthropic faked.

Nothing here touches the real Keychain or the network.
"""

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eatools import keystore  # noqa: E402

FAKE_KEY = "sk-ant-api03-" + "A1b2C3d4_-" * 4


class FakeSecurity:
    """Stands in for the `security` CLI, holding one item in memory."""

    def __init__(self, stored=None):
        self.stored = stored
        self.calls = []

    def __call__(self, args, stdin=None):
        self.calls.append((args, stdin))
        if args[0] == "find-generic-password":
            if self.stored is None:
                return subprocess.CompletedProcess(args, 44, "", "not found")
            return subprocess.CompletedProcess(args, 0, self.stored + "\n", "")
        if args == ["-i"]:
            self.stored = stdin.split('-w "', 1)[1].rsplit('"', 1)[0]
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[0] == "delete-generic-password":
            existed, self.stored = self.stored is not None, None
            return subprocess.CompletedProcess(args, 0 if existed else 44, "", "")
        raise AssertionError(f"unexpected security call {args}")


class Patched:
    """Swap keystore's `security` runner, availability and key check; restore env after."""

    def __init__(self, fake, verify_ok=True):
        self.fake, self.verify_ok = fake, verify_ok

    def __enter__(self):
        self.saved = (keystore._run, keystore.available, keystore.verify, keystore._loaded_from_keychain,
                      os.environ.pop("ANTHROPIC_API_KEY", None), os.environ.pop("ANTHROPIC_AUTH_TOKEN", None))
        keystore._run = self.fake
        keystore.available = lambda: True

        def verify(key):
            if not self.verify_ok:
                raise keystore.KeystoreError("Anthropic rejected this key.")

        keystore.verify = verify
        keystore._loaded_from_keychain = False
        return self.fake

    def __exit__(self, *exc):
        keystore._run, keystore.available, keystore.verify, keystore._loaded_from_keychain, key, token = self.saved
        os.environ.pop("ANTHROPIC_API_KEY", None)
        if key is not None:
            os.environ["ANTHROPIC_API_KEY"] = key
        if token is not None:
            os.environ["ANTHROPIC_AUTH_TOKEN"] = token


def _client(host="127.0.0.1"):
    from fastapi.testclient import TestClient

    from eatools.app import app

    return TestClient(app, client=(host, 50000))


# ---------------------------------------------------------------------------


def test_save_passes_key_on_stdin_never_argv():
    with Patched(FakeSecurity()) as fake:
        keystore.save(FAKE_KEY)

        assert fake.stored == FAKE_KEY
        for args, _ in fake.calls:
            assert FAKE_KEY not in " ".join(args), "key must not appear in process arguments"
        assert os.environ["ANTHROPIC_API_KEY"] == FAKE_KEY
        assert keystore.is_saved_key_active()


def test_malformed_key_is_refused_before_security_runs():
    # A quote would break out of the `security -i` command line.
    for bad in ["", "not-a-key", 'sk-ant-abcdefghijklmnopqrstuvwxyz" -s OTHER', "sk-ant-short"]:
        with Patched(FakeSecurity()) as fake:
            try:
                keystore.save(bad)
            except keystore.KeystoreError:
                pass
            else:
                raise AssertionError(f"accepted {bad!r}")
            assert fake.calls == []


def test_saved_key_is_loaded_on_start():
    with Patched(FakeSecurity(stored=FAKE_KEY)):
        assert keystore.load_into_env()
        assert os.environ["ANTHROPIC_API_KEY"] == FAKE_KEY
        assert keystore.is_saved_key_active()


def test_operator_env_key_wins_and_survives_forget():
    with Patched(FakeSecurity(stored=FAKE_KEY)):
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-operator-key-set-in-env-000000"
        assert not keystore.load_into_env()
        keystore.forget()
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-operator-key-set-in-env-000000"
        assert not keystore.is_saved_key_active()


def test_forget_removes_item_and_env_key():
    with Patched(FakeSecurity(stored=FAKE_KEY)) as fake:
        keystore.load_into_env()
        keystore.forget()
        assert fake.stored is None and "ANTHROPIC_API_KEY" not in os.environ
        keystore.forget()  # already gone is fine


def test_endpoint_saves_after_check_and_never_echoes_key():
    with Patched(FakeSecurity()) as fake:
        client = _client()
        res = client.post("/api/key", json={"key": FAKE_KEY}, headers={"X-EATools-Intent": "save-key"})

        assert res.status_code == 200, res.text
        assert FAKE_KEY not in res.text
        assert fake.stored == FAKE_KEY
        health = client.get("/api/health").json()
        assert health["credentials"] and health["saved_key"]
        assert FAKE_KEY not in str(health)


def test_endpoint_rejects_key_anthropic_refuses():
    with Patched(FakeSecurity(), verify_ok=False) as fake:
        res = _client().post("/api/key", json={"key": FAKE_KEY}, headers={"X-EATools-Intent": "save-key"})

        assert res.status_code == 400 and "rejected" in res.json()["detail"]
        assert fake.stored is None, "an unverified key must not be persisted"


def test_endpoint_requires_intent_header():
    """Without the header a cross-site form post could plant an attacker's key."""
    with Patched(FakeSecurity()) as fake:
        res = _client().post("/api/key", json={"key": FAKE_KEY})

        assert res.status_code == 403
        assert fake.stored is None


def test_endpoint_refuses_non_local_clients():
    with Patched(FakeSecurity(stored=FAKE_KEY)) as fake:
        client = _client(host="10.1.2.3")
        assert client.post("/api/key", json={"key": FAKE_KEY}, headers={"X-EATools-Intent": "save-key"}).status_code == 403
        assert client.delete("/api/key", headers={"X-EATools-Intent": "forget-key"}).status_code == 403
        assert fake.stored == FAKE_KEY


def test_forget_endpoint():
    with Patched(FakeSecurity(stored=FAKE_KEY)) as fake:
        keystore.load_into_env()
        res = _client().delete("/api/key", headers={"X-EATools-Intent": "forget-key"})

        assert res.status_code == 200 and res.json()["credentials"] is False
        assert fake.stored is None


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  OK  {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL  {name}: {exc}")
            except Exception as exc:  # noqa: BLE001 - surface the crash under test
                failures += 1
                print(f"FAIL  {name}: {type(exc).__name__}: {exc}")
    print("all passed" if not failures else f"{failures} failure(s)")
    sys.exit(1 if failures else 0)
