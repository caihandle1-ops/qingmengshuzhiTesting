"""Conservative company references shared by QA, history and workspace search.

The catalog supplies spellings, never the set of accessible companies. Every
match is constrained to the identities supplied by the caller's workspace.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import re

from core.builtin_catalog import builtin_aliases


def company_aliases(name: str) -> tuple[str, ...]:
    legacy = name.replace('股份有限公司', '').replace('新能源科技', '').replace('有限公司', '')
    return tuple(sorted({value for value in (name, legacy, *builtin_aliases(name)) if value},
                        key=lambda value: (-len(value), value)))


def alias_pattern(alias: str) -> str:
    escaped = re.escape(alias)
    # A ticker is a token, not a substring of a different ticker/order number.
    if re.fullmatch(r'\d{4,6}', alias):
        return r'(?<![A-Za-z0-9])' + escaped + r'(?![A-Za-z0-9])'
    if re.fullmatch(r'[A-Za-z0-9 ._-]+', alias):
        return r'(?<![A-Za-z0-9])' + escaped + r'(?![A-Za-z0-9])'
    return escaped


@dataclass(frozen=True)
class CompanyMention:
    start: int
    end: int
    names: tuple[str, ...]


def company_mentions(question: str, companies: Iterable[str],
                     extra_aliases: Mapping[str, Iterable[str]] | None = None) -> list[CompanyMention]:
    """Prefer the longest nonoverlapping reference, retaining alias ambiguity."""
    spans: dict[tuple[int, int], set[str]] = {}
    for name in dict.fromkeys(companies):
        aliases = (*company_aliases(name), *((extra_aliases or {}).get(name, ())))
        for alias in set(aliases):
            if not alias:
                continue
            for match in re.finditer(alias_pattern(alias), question, re.IGNORECASE):
                spans.setdefault(match.span(), set()).add(name)
    selected = []
    for (start, end), names in sorted(spans.items(), key=lambda item: (-(item[0][1] - item[0][0]), item[0][0])):
        if any(start < item.end and end > item.start for item in selected):
            continue
        selected.append(CompanyMention(start, end, tuple(sorted(names))))
    return sorted(selected, key=lambda item: item.start)


def explicit_company_names(question: str, companies: Iterable[str], extra_aliases=None) -> list[str]:
    return list(dict.fromkeys(mention.names[0] for mention in company_mentions(question, companies, extra_aliases)
                             if len(mention.names) == 1))


def ambiguous_company_names(question: str, companies: Iterable[str], extra_aliases=None) -> list[str]:
    return list(dict.fromkeys(name for mention in company_mentions(question, companies, extra_aliases)
                             if len(mention.names) > 1 for name in mention.names))


def strip_known_companies(question: str, companies: Iterable[str], extra_aliases=None) -> str:
    for mention in reversed(company_mentions(question, companies, extra_aliases)):
        if len(mention.names) == 1:
            question = question[:mention.start] + ' ' + question[mention.end:]
    return question


def company_search_matches(query: str, company: str) -> bool:
    """Search may offer partial names; ticker queries still need whole tokens."""
    needle = re.sub(r'\s+', '', query).casefold()
    for alias in company_aliases(company):
        normalized = re.sub(r'\s+', '', alias).casefold()
        if re.fullmatch(r'\d{4,6}', alias):
            if re.search(alias_pattern(alias), query):
                return True
        elif needle in normalized or (len(normalized) >= 2 and normalized in needle):
            return True
    return False
