"""A reference for every config field, derived from the models themselves.

The config has grown to six top-level sections and ~90 fields across the nested models, and the
only way to find out what exists has been to read `config.example.yaml` or the source. This walks
the Pydantic models and produces that list, so it cannot drift from what the code actually
accepts.

**Read-only, deliberately.** An editable form would have to serialize the loaded model back to
YAML — and the loaded model has already had `${VAR}` expanded, so saving it would write resolved
API keys and passwords into a file whose whole point is that it holds env *references* and is
therefore safe to edit and to keep. The raw YAML editor stays the only thing that writes.

Help text comes from the ``#:`` comments above each field. Pydantic does not read those (they are
a Sphinx convention, not `Field(description=...)`), so they are lifted from the source here —
which means adding a ``#:`` comment is all it takes to document a new field.
"""

from __future__ import annotations

import ast
import functools
import inspect
import textwrap
import typing
from dataclasses import dataclass

from pydantic import BaseModel
from pydantic_core import PydanticUndefined


@dataclass
class FieldDoc:
    name: str
    type: str
    default: str
    help: str


@dataclass
class SectionDoc:
    name: str  #: dotted path, e.g. "monitoring.docker"
    title: str  #: the model's class name
    summary: str  #: first paragraph of the model docstring
    fields: list[FieldDoc]


@functools.cache
def _comments_for(model: type[BaseModel]) -> dict[str, str]:
    """``#:`` comment blocks above each field in the model's source, keyed by field name."""
    try:
        src = inspect.getsource(model)
    except (OSError, TypeError):  # pragma: no cover - only for dynamically built models
        return {}
    lines = src.split("\n")
    out: dict[str, str] = {}
    try:
        # dedent, not cleandoc: cleandoc also strips leading blank lines, which would shift
        # every lineno by one and attach each comment to the wrong field.
        body = ast.parse(textwrap.dedent(src)).body[0].body  # type: ignore[attr-defined]
    except (SyntaxError, IndexError):  # pragma: no cover
        return {}
    for node in body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        collected: list[str] = []
        i = node.lineno - 2  # 0-based index of the line above the field
        while i >= 0 and lines[i].strip().startswith("#:"):
            collected.insert(0, lines[i].strip()[2:].strip())
            i -= 1
        if collected:
            out[node.target.id] = " ".join(collected)
    return out


def _type_name(annotation: object) -> str:
    """A short, readable rendering of a field's annotation."""
    if annotation is None:
        return "any"
    origin = typing.get_origin(annotation)
    if origin is not None:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        optional = len(args) != len(typing.get_args(annotation))
        inner = " | ".join(_type_name(a) for a in args) or "any"
        name = getattr(origin, "__name__", str(origin))
        if str(origin) in ("typing.Union", "<class 'types.UnionType'>") or name == "UnionType":
            return f"{inner}, optional" if optional else inner
        rendered = f"{name}[{inner}]"
        return f"{rendered}, optional" if optional else rendered
    return getattr(annotation, "__name__", str(annotation))


def _default_repr(field) -> str:
    if field.default is not PydanticUndefined and field.default is not None:
        value = field.default
    elif field.default_factory is not None:
        try:
            value = field.default_factory()
        except Exception:  # pragma: no cover - a factory needing arguments
            return ""
    elif field.default is None:
        return "none"
    else:
        return ""
    if isinstance(value, BaseModel):
        return ""  # a nested section: its own entry lists the details
    if isinstance(value, set):
        value = sorted(value)
    text = str(value)
    return text if len(text) <= 60 else text[:57] + "…"


def _is_model(annotation: object) -> type[BaseModel] | None:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        if isinstance(arg, type) and issubclass(arg, BaseModel):
            return arg
    return None


def describe(model: type[BaseModel], prefix: str = "") -> list[SectionDoc]:
    """Every section of the config, depth-first, in declaration order."""
    # __doc__, not inspect.getdoc: getdoc walks up to BaseModel and would label every
    # undocumented section with pydantic's own class docstring.
    docstring = inspect.cleandoc(model.__doc__ or "")
    section = SectionDoc(
        name=prefix or "(root)",
        title=model.__name__,
        summary=docstring.split("\n\n")[0].replace("\n", " ").strip(),
        fields=[],
    )
    comments = _comments_for(model)
    nested: list[SectionDoc] = []
    for name, field in model.model_fields.items():
        child = _is_model(field.annotation)
        if child is not None:
            nested.extend(describe(child, f"{prefix}.{name}" if prefix else name))
            continue
        section.fields.append(
            FieldDoc(
                name=name,
                type=_type_name(field.annotation),
                default=_default_repr(field),
                help=comments.get(name, ""),
            )
        )
    return ([section] if section.fields else []) + nested
