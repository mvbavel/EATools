"""Opt-in persistence of the Anthropic API key in the macOS Keychain.

The default key path is browser-only (sessionStorage + request header). This module is
the one deliberate exception: when the user clicks *Save to Keychain*, the key is stored
under the Keychain service ``ANTHROPIC_API_KEY`` for the current user and loaded into
the server's environment on every start. Nothing is written anywhere else, the key is
never logged or returned, and it reaches ``security`` on stdin -- never in argv, where
other processes could read it.
"""

from __future__ import annotations

import getpass
import logging
import os
import re
import shutil
import subprocess
import sys

import anthropic

log = logging.getLogger(__name__)

SERVICE = "ANTHROPIC_API_KEY"
ENV = "ANTHROPIC_API_KEY"
# Anthropic keys are sk-ant-<base64url>. Checking the shape also keeps anything that
# could break out of the quoted `security -i` command off stdin.
_KEY_RE = re.compile(r"^sk-ant-[A-Za-z0-9_-]{20,}$")

# True only when the environment key came from the Keychain, so Forget never removes a
# key the operator set themselves.
_loaded_from_keychain = False


class KeystoreError(Exception):
    """A short, user-safe message."""


def available() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None


def _account() -> str:
    return os.environ.get("USER") or getpass.getuser()


def _run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    """The single place `security` is invoked; replaced in tests."""
    return subprocess.run(["security", *args], input=stdin, capture_output=True, text=True, check=False)


def load_into_env() -> bool:
    """Put a saved key into the environment at startup, unless one is already set."""
    global _loaded_from_keychain
    if os.environ.get(ENV) or not available():
        return False
    res = _run(["find-generic-password", "-a", _account(), "-s", SERVICE, "-w"])
    key = res.stdout.strip()
    if res.returncode != 0 or not key:
        return False
    os.environ[ENV] = key
    _loaded_from_keychain = True
    return True


def is_saved_key_active() -> bool:
    return _loaded_from_keychain and bool(os.environ.get(ENV))


def valid_format(key: str) -> bool:
    return bool(_KEY_RE.match(key or ""))


def verify(key: str) -> None:
    """One cheap authenticated call, so a mistyped key is never persisted."""
    try:
        anthropic.Anthropic(api_key=key).models.list(limit=1)
    except anthropic.AuthenticationError:
        raise KeystoreError("Anthropic rejected this key.") from None
    except anthropic.APIConnectionError:
        raise KeystoreError("Could not reach the Anthropic API to check the key -- check connectivity.") from None
    except anthropic.APIError as exc:
        log.warning("Key check failed: %s", type(exc).__name__)
        raise KeystoreError("Could not check the key with Anthropic.") from None


def save(key: str) -> None:
    """Store (or replace) the key in the Keychain and use it from now on."""
    global _loaded_from_keychain
    if not available():
        raise KeystoreError("Saving the key needs the macOS Keychain, which is not available here.")
    if not valid_format(key):
        raise KeystoreError("That does not look like an Anthropic API key (sk-ant-...).")
    command = f'add-generic-password -U -a "{_account()}" -s "{SERVICE}" -w "{key}"\n'
    res = _run(["-i"], stdin=command)
    if res.returncode != 0 or res.stderr.strip():
        # stderr from `security` never contains the secret, but log only that it failed.
        log.warning("Keychain write failed (exit %s)", res.returncode)
        raise KeystoreError("Could not write to the Keychain.")
    os.environ[ENV] = key
    _loaded_from_keychain = True


def forget() -> None:
    """Remove the saved key from the Keychain and stop using it."""
    global _loaded_from_keychain
    if not available():
        raise KeystoreError("The macOS Keychain is not available here.")
    res = _run(["delete-generic-password", "-a", _account(), "-s", SERVICE])
    # Exit 44 = item not found: already forgotten, which is the goal.
    if res.returncode not in (0, 44):
        log.warning("Keychain delete failed (exit %s)", res.returncode)
        raise KeystoreError("Could not remove the key from the Keychain.")
    if _loaded_from_keychain:
        os.environ.pop(ENV, None)
        _loaded_from_keychain = False
