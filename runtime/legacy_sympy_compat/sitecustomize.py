"""Startup compatibility for old SymPy releases on the benchmark Python.

SymPy 1.0 imports collection ABCs from :mod:`collections` and its own test
runner promotes the resulting Python 3.9 deprecation warning to an exception.
Providing the aliases at interpreter startup preserves the old public runtime
contract without editing the buggy or fixed benchmark checkout.
"""

from __future__ import annotations

import collections
import collections.abc


for _name in (
    "AsyncGenerator",
    "AsyncIterable",
    "AsyncIterator",
    "Awaitable",
    "ByteString",
    "Callable",
    "Collection",
    "Container",
    "Coroutine",
    "Generator",
    "Hashable",
    "ItemsView",
    "Iterable",
    "Iterator",
    "KeysView",
    "Mapping",
    "MappingView",
    "MutableMapping",
    "MutableSequence",
    "MutableSet",
    "Reversible",
    "Sequence",
    "Set",
    "Sized",
    "ValuesView",
):
    if _name not in collections.__dict__ and hasattr(collections.abc, _name):
        setattr(collections, _name, getattr(collections.abc, _name))
