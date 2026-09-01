"""Reading HTML form submissions off a WSGI request.

The stdlib used to cover this with ``cgi.FieldStorage``, which was removed in
Python 3.13, so the little that is needed is here: urlencoded bodies and
``multipart/form-data`` uploads.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs

#: Refuse anything larger rather than reading it into memory. A dispatch sheet
#: for one day is a few hundred kilobytes.
MAX_BODY_BYTES = 25 * 1024 * 1024


class FormError(Exception):
    """Raised when a request body cannot be read as a form."""


@dataclass(frozen=True)
class Field:
    name: str
    value: bytes
    filename: Optional[str] = None
    content_type: str = ""

    @property
    def text(self) -> str:
        return self.value.decode("utf-8", "replace").strip()

    @property
    def is_file(self) -> bool:
        return self.filename is not None


class Form:
    """The fields of one submission, keyed by name."""

    def __init__(self, fields: list[Field]):
        self.fields = fields
        self._by_name: dict[str, Field] = {}
        for field in fields:
            self._by_name.setdefault(field.name, field)

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    def get(self, name: str, default: str = "") -> str:
        field = self._by_name.get(name)
        return field.text if field is not None else default

    def file(self, name: str) -> Optional[Field]:
        field = self._by_name.get(name)
        return field if field is not None and field.is_file and field.value else None

    def checked(self, name: str) -> bool:
        return name in self._by_name

    def number(self, name: str, default: float) -> float:
        try:
            return float(self.get(name))
        except (TypeError, ValueError):
            return default

    def items(self):
        return [(field.name, field) for field in self.fields]


def read_form(environ) -> Form:
    """Read the request body of a WSGI ``environ`` as a form."""
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        raise FormError("malformed Content-Length")
    if length < 0:
        raise FormError("malformed Content-Length")
    if length > MAX_BODY_BYTES:
        raise FormError(f"upload is larger than {MAX_BODY_BYTES // (1024 * 1024)} MB")

    body = environ["wsgi.input"].read(length) if length else b""
    return parse_form(body, environ.get("CONTENT_TYPE", ""))


def parse_form(body: bytes, content_type: str) -> Form:
    kind = content_type.split(";", 1)[0].strip().lower()
    if kind == "multipart/form-data":
        return Form(_parse_multipart(body, content_type))
    if kind == "application/x-www-form-urlencoded" or not kind:
        pairs = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
        return Form([Field(name, value.encode("utf-8")) for name, values in pairs.items() for value in values])
    raise FormError(f"unsupported content type {kind!r}")


def _boundary(content_type: str) -> bytes:
    for parameter in content_type.split(";")[1:]:
        key, _, value = parameter.strip().partition("=")
        if key.strip().lower() == "boundary":
            value = value.strip().strip('"')
            if value:
                return value.encode("ascii", "replace")
    raise FormError("multipart body has no boundary")


def _parse_multipart(body: bytes, content_type: str) -> list[Field]:
    marker = b"--" + _boundary(content_type)
    fields: list[Field] = []

    for chunk in body.split(marker):
        if chunk in (b"", b"--", b"--\r\n", b"\r\n"):
            continue
        chunk = chunk.lstrip(b"\r\n")
        head, separator, payload = chunk.partition(b"\r\n\r\n")
        if not separator:
            continue
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]

        name = filename = None
        part_type = ""
        for line in head.decode("utf-8", "replace").splitlines():
            key, _, value = line.partition(":")
            key = key.strip().lower()
            if key == "content-disposition":
                parameters = _disposition(value)
                name = parameters.get("name")
                filename = parameters.get("filename")
            elif key == "content-type":
                part_type = value.strip()

        if name:
            fields.append(Field(name=name, value=payload, filename=filename, content_type=part_type))
    return fields


def _disposition(value: str) -> dict[str, str]:
    parameters: dict[str, str] = {}
    for parameter in value.split(";")[1:]:
        key, _, raw = parameter.strip().partition("=")
        parameters[key.strip().lower()] = raw.strip().strip('"')
    return parameters
