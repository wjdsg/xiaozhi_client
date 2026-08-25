"""Local Chinese character-to-example-word lookup for dictation prompts."""

from __future__ import annotations

import json
import math
import re
import threading
from pathlib import Path
from typing import Iterable

# Web 框架适配已迁到 web.py；本文件只保留可复用的字典模型。


_CJK_CHAR = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]$")
_CJK_WORD = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]{2,4}$")
_EXCLUDED_POS = {"nr", "nrfg", "nrt", "ns", "nt", "nz", "eng", "x"}

# Small, auditable overrides for frequent polyphonic/ambiguous primary-school
# characters. The general vocabulary still comes from the local dictionaries.
_CURATED = {
    "好": "好事", "行": "行走", "长": "长大", "乐": "快乐",
    "重": "重要", "还": "还有", "只": "一只", "数": "数学",
    "种": "种子", "觉": "觉得", "空": "天空", "得": "得到",
    "为": "因为", "发": "发现", "当": "当然", "了": "了解",
    "地": "大地", "的": "好的", "着": "看着", "和": "和平",
}


class CharacterDictionary:
    """Lazy in-memory example index built from textbook and Jieba data."""

    def __init__(self, catalog_path: Path, jieba_path: Path) -> None:
        self.catalog_path = Path(catalog_path)
        self.jieba_path = Path(jieba_path)
        self._examples: dict[str, str] | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _score(word: str, char: str, frequency: int, textbook: bool) -> float:
        score = math.log10(max(1, frequency) + 1)
        if textbook:
            score += 20
        if word.startswith(char):
            score += 3
        if len(word) == 2:
            score += 3
        elif len(word) == 3:
            score += 1
        return score

    def _textbook_words(self) -> set[str]:
        data = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        words: set[str] = set()
        for grade in data.get("grades", []):
            for unit in grade.get("units", []):
                words.update(word for word in unit.get("vocabulary", []) if _CJK_WORD.fullmatch(word))
                for lesson in unit.get("lessons", []):
                    words.update(word for word in lesson.get("words", []) if _CJK_WORD.fullmatch(word))
        return words

    def _build(self) -> dict[str, str]:
        best: dict[str, tuple[float, str]] = {}
        for word in self._textbook_words():
            for char in set(word):
                score = self._score(word, char, 1, True)
                if score > best.get(char, (-1, ""))[0]:
                    best[char] = (score, word)

        if self.jieba_path.is_file():
            for line in self.jieba_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                fields = line.split()
                if len(fields) < 2 or not _CJK_WORD.fullmatch(fields[0]):
                    continue
                word = fields[0]
                pos = fields[2] if len(fields) > 2 else ""
                if pos in _EXCLUDED_POS:
                    continue
                try:
                    frequency = int(fields[1])
                except ValueError:
                    continue
                for char in set(word):
                    score = self._score(word, char, frequency, False)
                    if score > best.get(char, (-1, ""))[0]:
                        best[char] = (score, word)

        examples = {char: word for char, (_score, word) in best.items()}
        examples.update(_CURATED)
        return examples

    def lookup(self, char: str) -> str | None:
        if not _CJK_CHAR.fullmatch(char):
            return None
        if self._examples is None:
            with self._lock:
                if self._examples is None:
                    self._examples = self._build()
        return self._examples.get(char)

    def batch(self, chars: Iterable[str]) -> dict[str, dict[str, str]]:
        result = {}
        for char in dict.fromkeys(str(item).strip() for item in chars):
            word = self.lookup(char)
            if word:
                result[char] = {"word": word, "spokenText": f"{word}的{char}"}
        return result
