"""Lexical identity preconditions shared by grouping and qualification."""

SCHEMA = 'meaningful-lexical-fingerprint/1'


def meaningful(tokens):
    """Punctuation fragments cannot establish source identity.

    Keep short meaningful code, including identifiers, literals and non-ASCII
    identifiers. This is an identity prerequisite, never security evidence.
    """
    return any(any(character.isalnum() or character in '_$' for character in token)
               for token in tokens)
