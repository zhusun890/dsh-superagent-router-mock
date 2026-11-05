"""Ids and word counting as dsh-superagent computes them (src/core/ids.ts, src/infra/text.ts)."""
from __future__ import annotations

import hashlib
import json
import logging
import warnings

with warnings.catch_warnings():
    warnings.simplefilter('ignore', SyntaxWarning)  # jieba 0.42 has invalid escape sequences
    import jieba

jieba.setLogLevel(logging.WARNING)

IM_WORDS = 100
NOTE_WORDS = 30


def key(*values: str) -> str:
    """sha256(JSON.stringify(values)) as hex, first 32 characters."""
    text = json.dumps(list(values), ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:32]


def word_count(text: str) -> int:
    """Stand-in for Intl.Segmenter('zh', {granularity: 'word'}) counting word-like segments: jieba tokens with a letter or digit."""
    return sum(1 for token in jieba.cut(text) if any(char.isalnum() for char in token))
