"""The CSS selector subset of tests/pagecheck.py: compile a selector and match it
against parsed elements (anything with tag, attrs, classes and parent)."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.pagecheck import Node

_PART = re.compile(r"""
    (?P<tag>[a-zA-Z][\w-]*|\*)
  | \.(?P<cls>[\w-]+\*?)
  | \#(?P<id>[\w-]+)
  | \[\s*(?P<attr>[\w:-]+)\s*(?:(?P<op>[\^$*~]?=)\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\]\s]+))\s*)?\]
""", re.X)


class Compound:
    """One simple selector: tag, classes, id and attribute tests, all of which must hold."""

    def __init__(self, source: str) -> None:
        self.tag: str | None = None
        self.tests: list[tuple[str, str | None, str | None]] = []
        pos = 0
        while pos < len(source):
            m = _PART.match(source, pos)
            if not m:
                raise ValueError(f"unsupported selector: {source!r}")
            if m["tag"]:
                self.tag = None if m["tag"] == "*" else m["tag"].lower()
            elif m["cls"]:
                cls = m["cls"]
                self.tests.append(("class^", None, cls[:-1]) if cls.endswith("*") else ("class", None, cls))
            elif m["id"]:
                self.tests.append(("id", "=", m["id"]))
            else:
                value = m["dq"] if m["dq"] is not None else m["sq"] if m["sq"] is not None else m["bare"]
                self.tests.append((m["attr"].lower(), m["op"], value))
            pos = m.end()

    def matches(self, node: Node) -> bool:
        if self.tag and node.tag != self.tag:
            return False
        for name, op, value in self.tests:
            if name == "class":
                if value not in node.classes:
                    return False
            elif name == "class^":
                if not any(c.startswith(value or "") for c in node.classes):
                    return False
            else:
                have = node.attrs.get(name)
                if have is None:
                    return False
                if op is None:
                    continue
                if op == "=" and have != value:
                    return False
                if op == "^=" and not have.startswith(value or ""):
                    return False
                if op == "$=" and not have.endswith(value or ""):
                    return False
                if op == "*=" and (value or "") not in have:
                    return False
                if op == "~=" and value not in have.split():
                    return False
        return True


def compile_selector(selector: str) -> list[list[tuple[str, Compound]]]:
    """Comma groups of (combinator, compound) chains; the first combinator is ' '."""
    groups = []
    for group in selector.split(","):
        tokens = re.findall(r'>|(?:[^\s>"\'\[]+|\[[^\]]*\])+', group.strip())
        chain: list[tuple[str, Compound]] = []
        combinator = " "
        for token in tokens:
            if token == ">":
                combinator = ">"
                continue
            chain.append((combinator, Compound(token)))
            combinator = " "
        if not chain:
            raise ValueError(f"empty selector: {selector!r}")
        groups.append(chain)
    return groups


def chain_matches(node: Node, chain: list[tuple[str, Compound]], scope: Node) -> bool:
    """Right-to-left match of a chain against node, never looking above scope."""
    combinator, last = chain[-1]
    if not last.matches(node):
        return False
    if len(chain) == 1:
        return True
    rest = chain[:-1]
    parent = node.parent
    if combinator == ">":
        return parent is not None and parent is not scope and chain_matches(parent, rest, scope)
    while parent is not None and parent is not scope:
        if chain_matches(parent, rest, scope):
            return True
        parent = parent.parent
    return False
