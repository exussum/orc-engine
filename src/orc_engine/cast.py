import base64
import builtins
import importlib
import re
from collections.abc import Callable, Mapping
from datetime import time
from types import MappingProxyType, ModuleType
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes

_NO_OBJECTS: Mapping[str, Any] = MappingProxyType({})
_KEY32 = re.compile(r"[A-Za-z0-9_-]{43}=?")
_HEX32 = re.compile(r"[0-9A-Fa-f]{64}")
_FQDN_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")  # 1-63 chars, no leading/trailing hyphen
_ERR_PARAMS = "Invalid parameter {}={!r}"
_ERR_MODULE = "Cannot load module {!r}: {}. Expected an importable module like 'orc.dal.mqtt.stub'."
_ERR_FUNCTION = "Cannot load function {!r}: {}. Expected a fully qualified callable like 'orc.plugins.my_plugin'."
_ERR_TIME = "Invalid time {!r}: expected HH:MM"


# scalar() calls a caster as (value, objects) and each() as (value,), so casters
# used by both take a trailing `objects` with a default.
def module(value: str, objects: Mapping[str, Any] = _NO_OBJECTS) -> ModuleType:
    try:
        return importlib.import_module(value)  # nosemgrep: non-literal-import
    except Exception as exc:
        raise ValueError(_ERR_MODULE.format(value, exc)) from exc


def float(value: str, objects: Mapping[str, Any] = _NO_OBJECTS) -> builtins.float:
    return builtins.float(value)


def int(value: str, objects: Mapping[str, Any] = _NO_OBJECTS) -> builtins.int:
    return builtins.int(value)


def bool(value: str, objects: Mapping[str, Any] = _NO_OBJECTS) -> builtins.bool:
    if value in ("True", "False"):
        return value == "True"
    raise ValueError(_ERR_PARAMS.format("bool", value))


def fqdn(value: str, objects: Mapping[str, Any] = _NO_OBJECTS) -> str:
    labels = value.split(".")
    if len(value) <= 253 and len(labels) >= 2 and all(map(_FQDN_LABEL.fullmatch, labels)):
        return value
    raise ValueError(_ERR_PARAMS.format("fqdn", value))


def nonblank(value: str) -> str:
    if value:
        return value
    raise ValueError(_ERR_PARAMS.format("nonblank", value))


def url(value: str) -> str:
    parts = urlparse(value)
    if parts.scheme in ("http", "https") and parts.netloc:
        return value
    raise ValueError(_ERR_PARAMS.format("url", value))


def uuid(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise ValueError(_ERR_PARAMS.format("uuid", value)) from None


def key32(value: str) -> bytes:
    if _KEY32.fullmatch(value):
        return base64.urlsafe_b64decode(value.rstrip("=") + "=")
    raise ValueError(_ERR_PARAMS.format("key32", value))


def hex32(value: str) -> bytes:
    if _HEX32.fullmatch(value):
        return bytes.fromhex(value)
    raise ValueError(_ERR_PARAMS.format("hex32", value))


def instance[T](value: object, cls: type[T]) -> T:
    if isinstance(value, cls):
        return value
    raise TypeError(f"expected {cls.__name__}, got {type(value).__name__}")


def resolve_function(value: str) -> Callable[..., Any]:
    try:
        module_path, fn_name = value.rsplit(".", 1)
        return getattr(importlib.import_module(module_path), fn_name)  # nosemgrep: non-literal-import
    except Exception as exc:
        raise ValueError(_ERR_FUNCTION.format(value, exc)) from exc


def clock(value: str) -> time:
    parts = value.split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(_ERR_TIME.format(value))
    hour, minute = builtins.int(parts[0]), builtins.int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(_ERR_TIME.format(value))
    return time(hour, minute)


def pem_cert(value: str) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(value.encode())
    except ValueError:
        raise ValueError(_ERR_PARAMS.format("pem_cert", value)) from None


def pem_key(value: str) -> PrivateKeyTypes:
    try:
        return serialization.load_pem_private_key(value.encode(), None)
    except ValueError, TypeError:
        raise ValueError(_ERR_PARAMS.format("pem_key", value)) from None
