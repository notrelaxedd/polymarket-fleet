"""Page lookups for the dashboard tests: find elements by data-* hooks and class
prefixes, read their text and attributes, so a restyle does not break the tests.

Stdlib only (html.parser). `page(html)` parses a page or a fragment into a small tree;
every `Node` answers CSS-like selectors with this subset:

    tag  *  .class  .prefix-*  #id  [attr]  [attr=v]  [attr="v"]  [attr^=v]  [attr$=v]
    [attr*=v]  [attr~=v]   descendant (space) and child (>) combinators, groups (a, b)

`.chip-*` matches any class that starts with "chip-". Text is the element's text with
entities decoded and whitespace collapsed to single spaces (`&middot;` reads as "·").

`shows_pnl(client, mode, money)` checks a mode's P&L is on view (top bar or Trading).

The hooks come from the step 7 component contract (docs/UI.md): data-page, data-stat,
data-row + data-id, data-chip, data-action, data-key, data-card, data-list, data-nav,
data-banner, data-field, data-form, data-flash.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Iterator

from tests.pagecheck_select import Compound, chain_matches, compile_selector

VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())


def _norm(text: str) -> str:
    return " ".join(text.split())


class Node:
    """An element (or the document root, tag "#root") with its attributes and children."""

    def __init__(self, tag: str, attrs: dict[str, str], parent: Node | None, source: str, start: int) -> None:
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Node | str] = []
        self._source = source
        self.start = start
        self.end = start

    def __repr__(self) -> str:
        hooks = " ".join(f'{k}="{v}"' for k, v in self.attrs.items() if k.startswith("data-") or k in ("id", "class"))
        return f"<{self.tag} {hooks}>"

    # ---- reading
    @property
    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    def has_class(self, name: str) -> bool:
        return name in self.classes

    def attr(self, name: str, default: str | None = None) -> str | None:
        return self.attrs.get(name, default)

    def has_attr(self, name: str) -> bool:
        """A boolean attribute such as checked, selected, open or disabled."""
        return name in self.attrs

    @property
    def text(self) -> str:
        """Text content, entities decoded, whitespace collapsed."""
        return _norm("".join(self._texts()))

    def _texts(self) -> Iterator[str]:
        for child in self.children:
            if isinstance(child, str):
                yield child
            else:
                yield from child._texts()

    @property
    def html(self) -> str:
        """The raw source of this element (start tag to end tag)."""
        return self._source[self.start:self.end]

    @property
    def is_open(self) -> bool:
        """A <details> that renders open."""
        return "open" in self.attrs

    @property
    def is_current(self) -> bool:
        """A nav link marked as the current page (aria-current, or the older .active)."""
        return self.attrs.get("aria-current") == "page" or self.has_class("active")

    @property
    def disabled(self) -> bool:
        return "disabled" in self.attrs

    @property
    def target(self) -> str | None:
        """Where an action goes: a form's action, a link's href, a button's formaction or
        its form's action."""
        for name in ("formaction", "action", "href"):
            if name in self.attrs:
                return self.attrs[name]
        form = self.closest("form")
        return form.attrs.get("action") if form else None

    # ---- finding
    def iter(self) -> Iterator[Node]:
        """Every descendant element in document order."""
        for child in self.children:
            if isinstance(child, Node):
                yield child
                yield from child.iter()

    def select(self, selector: str) -> list[Node]:
        groups = compile_selector(selector)
        return [n for n in self.iter() if any(chain_matches(n, chain, self) for chain in groups)]

    def first(self, selector: str) -> Node | None:
        found = self.select(selector)
        return found[0] if found else None

    def one(self, selector: str) -> Node:
        """Exactly one match, else an AssertionError naming the count."""
        found = self.select(selector)
        assert len(found) == 1, f"{selector!r}: {len(found)} matches, wanted 1"
        return found[0]

    def has(self, selector: str) -> bool:
        return bool(self.select(selector))

    def count(self, selector: str) -> int:
        return len(self.select(selector))

    def texts(self, selector: str) -> list[str]:
        return [n.text for n in self.select(selector)]

    def closest(self, selector: str) -> Node | None:
        """The nearest ancestor (or self) matching a single compound selector."""
        compound = Compound(selector)
        node: Node | None = self
        while node is not None and node.tag != "#root":
            if compound.matches(node):
                return node
            node = node.parent
        return None

    @property
    def hrefs(self) -> list[str]:
        """Every link target below this node."""
        return [n.attrs["href"] for n in self.select("[href]")]

    # ---- contract hooks
    def row(self, kind: str, id: object) -> Node:
        return self.one(f'[data-row="{kind}"][data-id="{id}"]')

    def rows(self, kind: str) -> list[Node]:
        return self.select(f'[data-row="{kind}"]')

    def row_ids(self, kind: str) -> list[str]:
        return [n.attrs["data-id"] for n in self.rows(kind)]

    def card(self, name: str) -> Node:
        """A section by data-card, or a disclosure whose data-key ends in -name."""
        found = self.select(f'[data-card="{name}"]') or self.select(f'details[data-key$="-{name}"]')
        assert len(found) == 1, f"card {name!r}: {len(found)} matches, wanted 1"
        return found[0]

    def cards(self) -> list[str]:
        return [n.attrs["data-card"] for n in self.select("[data-card]")]

    def listing(self, name: str) -> Node:
        return self.one(f'[data-list="{name}"]')

    def field(self, key: str) -> Node:
        """The wrapper of a settings input (data-field="<settings key>")."""
        return self.one(f'[data-field="{key}"]')

    def input(self, name: str) -> Node:
        """The one input, select or textarea named name."""
        return self.one(f'input[name="{name}"], select[name="{name}"], textarea[name="{name}"]')

    def form(self, name: str) -> Node:
        return self.one(f'[data-form="{name}"]')

    def action(self, verb: str) -> Node:
        return self.one(f'[data-action="{verb}"]')

    def actions(self) -> list[str]:
        return [n.attrs["data-action"] for n in self.select("[data-action]")]

    def chip(self, state: str) -> Node:
        return self.one(f'[data-chip="{state}"]')

    def chips(self) -> list[str]:
        return [n.attrs["data-chip"] for n in self.select("[data-chip]")]

    def chip_texts(self) -> list[str]:
        return [n.text for n in self.select("[data-chip]")]

    def stat(self, name: str) -> Node:
        return self.one(f'[data-stat="{name}"]')

    def nav(self, name: str) -> Node:
        return self.one(f'[data-nav="{name}"]')

    def prop(self, label: str) -> str:
        """The value next to a label: the <dd> after a <dt>, or the .stat-value of a
        .stat whose .stat-label reads label."""
        for dt in self.select("dt"):
            if dt.text == label:
                dd = dt.next_element()
                assert dd is not None and dd.tag == "dd", f"no <dd> after <dt>{label}</dt>"
                return dd.text
        for stat in ([self] if self.has_class("stat") else []) + self.select(".stat"):
            if any(n.text == label for n in stat.select(".stat-label")):
                return " ".join(n.text for n in stat.select(".stat-value"))
        raise AssertionError(f"no value labelled {label!r}")

    def next_element(self) -> Node | None:
        siblings = self.parent.children if self.parent else []
        after = False
        for child in siblings:
            if child is self:
                after = True
            elif after and isinstance(child, Node):
                return child
        return None

    # ---- page frame
    @property
    def page_name(self) -> str | None:
        main = self.first("[data-page]")
        return main.attrs["data-page"] if main else None

    @property
    def flash(self) -> str | None:
        box = self.first("[data-flash]")
        return box.text if box else None

    def errors(self) -> list[str]:
        """The text of every inline error on the page."""
        return self.texts(".error")


class _Builder(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.line_starts = [0] + [m.end() for m in re.finditer(r"\n", source)]
        self.root = Node("#root", {}, None, source, 0)
        self.root.end = len(source)
        self.stack = [self.root]

    def _offset(self) -> int:
        line, col = self.getpos()
        return self.line_starts[line - 1] + col

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        start = self._offset()
        node = Node(tag, {k: ("" if v is None else v) for k, v in attrs}, self.stack[-1], self.source, start)
        node.end = start + len(self.get_starttag_text() or "")
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                start = self._offset()
                close = self.source.find(">", start)
                for node in self.stack[depth:]:
                    node.end = close + 1 if close >= 0 else len(self.source)
                del self.stack[depth:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def page(html: str) -> Node:
    """Parse a page or a fragment; the result is the document root."""
    builder = _Builder(html)
    builder.feed(html)
    builder.close()
    return builder.root


def fleet_html(client) -> str:  # noqa: ANN001 - a TestClient or an httpx.Client
    """The Fleet card page: /fleet/list since the 3D page took /fleet, else / (steps 1 to 6)."""
    r = client.get("/fleet/list")
    return r.text if r.status_code == 200 else client.get("/").text


def topbar(doc: Node) -> Node:
    """The top bar of a page, or the whole /fragments/topbar fragment."""
    return doc.first("#topbar") or doc


def mode_pill(doc: Node) -> str:
    """The text of the top bar's mode pill: PAPER or LIVE."""
    pill = topbar(doc).first(".pill")
    assert pill is not None, "no mode pill in the top bar"
    return pill.text


def shows_pnl(client, mode: str, money: str) -> bool:  # noqa: ANN001 - a TestClient or an httpx.Client
    """True when a mode's P&L figure `money` (such as "$1.76") is on view: in the top bar
    ("paper today $1.76", step 7 "paper +$1.76 today"), or, from step 7 on, as the
    Trading page stat data-stat="<mode>-today" (the other mode's P&L moves there)."""
    bar = topbar(page(client.get("/fragments/topbar").text)).text
    if re.search(rf"\b{mode}\b[^$]*?[+-]?{re.escape(money)}", bar):
        return True
    stat = page(client.get("/trading").text).first(f'[data-stat="{mode}-today"]')
    return stat is not None and money in stat.text
