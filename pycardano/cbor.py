"""Centralized cbor2 import."""

import cbor2  # noqa: F401

# cbor2pure, installed with the ``cbor2pure`` extra, is cbor2's decoder with the
# array and indefinite-length hooks that preserve the original encoding during a
# single decode pass. Its decoded values and its exceptions are cbor2's own classes,
# and encoding always uses cbor2. See pycardano.serialization.loads.
try:
    import cbor2pure  # noqa: F401
except ImportError:  # the optional extra is not installed
    cbor2pure = None  # type: ignore[assignment]
