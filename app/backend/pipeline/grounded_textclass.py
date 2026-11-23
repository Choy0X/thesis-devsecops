"""The weakness class of a fix that the advisory does not classify, read from the words of the fix itself.

Linux kernel advisories (OSV `Linux`) carry no CWE: the text is the commit message ("net/sched: fix use-after-free in act_ct"). A fix commit usually states the weakness it
removes, so a small, fixed rule table maps those words to the classes of the catalogue. Deterministic, no model call. The result is a weaker label than a CWE id the advisory
lists, so a unit records `class_basis: text_rule`, and every report keeps the two apart.

The text is searched in this order and the first source with a match decides (every class it names, in order of appearance): the first line of the commit message, the advisory
summary, the rest of the commit message, the advisory details. A kernel report quoted in the body (KASAN, UBSAN) often names several weaknesses at once; the subject line is
what the author says the commit fixes.
"""
from __future__ import annotations

import re

# (class, pattern); the first pattern that matches in a source decides the order, every distinct class found in that source is returned
RULES = (
    ('CWE-416', r'\buse[- ]after[- ]free\b|\buaf\b|\bdouble[- ]free\b|\bfreed (?:twice|too early)\b|\bdangling (?:pointer|reference)\b'),
    ('CWE-787', r'\bout[- ]of[- ]bounds? write\b|\boob[- ]write\b|\b(?:heap|stack|slab|buffer)[- ]overflow\b|\boverflow(?:s|ing)? (?:the |a )?(?:heap|stack|buffer|array)\b|\bbuffer overrun\b'),
    ('CWE-125', r'\bout[- ]of[- ]bounds? read\b|\boob[- ]read\b|\bover-?read\b|\bread (?:beyond|past) \b'),
    ('CWE-119', r'\bout[- ]of[- ]bounds?\b|\boob\b|\bbuffer overflow\b|\bmemory corruption\b'),
    ('CWE-476', r'\bnull[- ]?(?:pointer )?(?:dereference|deref|ptr[- ]deref)\b|\bnpe\b|\bdereferenc\w* (?:a )?null\b'),
    ('CWE-401', r'\bmemory leaks?\b|\bleaks? (?:of )?(?:the )?(?:memory|skb|buffer|page|pages)\b|\bleaked memory\b|\bresource leak\b'),
    ('CWE-190', r'\binteger (?:overflow|underflow|wrap(?:around)?)\b|\barithmetic overflow\b|\bwrap-?around\b|\bunderflow\b'),
    ('CWE-369', r'\bdivi(?:de|sion)[- ]by[- ]zero\b|\bdivision by 0\b|\bdivide by 0\b'),
    ('CWE-457', r'\buninit(?:ialized|ialised)?(?:[- ]value| (?:variable|memory|value|data|stack))\b|\buse of uninit\w*\b|\bkmsan\b'),
    ('CWE-362', r'\brace condition\b|\bdata[- ]races?\b|\brace (?:between|with)\b|\braces (?:with|against)\b|\bracy\b|\bconcurrent (?:access|modification)\b|\btoctou\b'),
    ('CWE-200', r'\binfo(?:rmation)? ?leaks?\b|\binfoleaks?\b|\bleaks? (?:kernel |uninitialized )?(?:stack|heap|kernel) (?:memory|data|content\w*|addresses|pointers?)\b'),
    ('CWE-617', r'\breachable assertion\b|\bbug_on\b.{0,40}\b(?:trigger\w*|reach\w*)\b'),
)
_COMPILED = tuple((cls, re.compile(pattern, re.I)) for cls, pattern in RULES)


def _classes_in(text):
    """(position, class) of the first match of every rule, ordered by position: the classes a piece of text names."""
    found = []
    for cls, rx in _COMPILED:
        m = rx.search(text or '')
        if m:
            found.append((m.start(), cls))
    found.sort()
    out = []
    for _, cls in found:
        if cls not in out:
            out.append(cls)
    return out


def _more_specific(classes):
    """CWE-119 is the parent of the two bounds classes; drop it when a child is named too."""
    if 'CWE-119' in classes and ('CWE-787' in classes or 'CWE-125' in classes):
        return [c for c in classes if c != 'CWE-119']
    return classes


def classes_from_text(commit_message, summary='', details=''):
    """The classes a fix names in its own words, [] when it names none. See the module text for the order of the sources."""
    message = commit_message or ''
    first, _, rest = message.partition('\n')
    for source in (first, summary, rest, details):
        found = _more_specific(_classes_in(source))
        if found:
            return found
    return []
