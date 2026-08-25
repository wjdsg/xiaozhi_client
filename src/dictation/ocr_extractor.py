"""Photo-to-dictation extraction with a lazy, single-instance OCR backend.

Parsing functions in this module only use the Python standard library, so they
can be tested without loading OpenCV, ONNX Runtime, or OCR model weights.
"""

from __future__ import annotations

import importlib.util
import math
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


SUPPORTED_MODES = {"zh", "zh_char", "zh_word", "en_vocab"}
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_CJK_WORD_RE = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]{2,6}$")
_EN_ENTRY_RE = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*(?:\s+[A-Za-z]+(?:['’-][A-Za-z]+)*)*")
_UNIT_RE = re.compile(r"^(?:unit|appendix|words?\s+in\s+each\s+unit)\b", re.I)
_PAGE_RE = re.compile(r"\bp\.?\s*\d+\b", re.I)
_LESSON_PREFIX_RE = re.compile(r"^\s*(?:第\s*)?\d+\s*(?:课)?[.、:：]?\s*")
_DICT_EXCLUDED_POS = {"nr", "nrfg", "nrt", "ns", "nt", "nz", "eng", "x"}


class ChineseLexiconSegmenter:
    """Small lazy Viterbi segmenter backed by the vendored Jieba lexicon."""

    def __init__(self, dictionary_path: str | Path | None) -> None:
        self.dictionary_path = Path(dictionary_path) if dictionary_path else None
        self._lexicon: dict[str, int] | None = None
        self._idioms: dict[str, int] | None = None
        self._lock = threading.Lock()

    def _load(self) -> dict[str, int]:
        if self._lexicon is not None:
            return self._lexicon
        with self._lock:
            if self._lexicon is not None:
                return self._lexicon
            lexicon: dict[str, int] = {}
            idioms: dict[str, int] = {}
            if self.dictionary_path and self.dictionary_path.is_file():
                for line in self.dictionary_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    fields = line.split()
                    if len(fields) < 2 or not re.fullmatch(r"[\u3400-\u9fff]{2,6}", fields[0]):
                        continue
                    # OCR word tables may legitimately contain names/places,
                    # and Jieba also mis-tags ordinary words such as 余波 and
                    # 人山人海. Keep all CJK entries here; the confirmation UI
                    # remains the final correctness boundary.
                    try:
                        frequency = max(1, int(fields[1]))
                        lexicon[fields[0]] = frequency
                        if len(fields[0]) == 4 and len(fields) >= 3 and fields[2] in {"i", "l"}:
                            idioms[fields[0]] = frequency
                    except ValueError:
                        continue
            self._lexicon = lexicon
            self._idioms = idioms
            return lexicon

    def segment(self, text: str) -> list[str]:
        text = "".join(_CJK_RE.findall(text))
        if len(text) <= 4:
            return [text] if text else []
        lexicon = self._load()
        if not lexicon:
            return list(text)
        size = len(text)
        best: list[tuple[float, list[str]]] = [(-10**18, []) for _ in range(size + 1)]
        best[size] = (0.0, [])
        for start in range(size - 1, -1, -1):
            # Unknown characters remain separate instead of becoming a giant
            # phrase; the confirmation screen can still remove them.
            options = [(best[start + 1][0] - 7.0, [text[start]] + best[start + 1][1])]
            for end in range(start + 2, min(size, start + 6) + 1):
                word = text[start:end]
                frequency = lexicon.get(word)
                if frequency:
                    score = math.log(frequency + 1) + 1.15 * (len(word) - 1) + best[end][0]
                    options.append((score, [word] + best[end][1]))
            best[start] = max(options, key=lambda item: item[0])
        return best[0][1]

    def contains(self, word: str) -> bool:
        return bool(_CJK_WORD_RE.fullmatch(word) and word in self._load())

    def correct_idiom(self, word: str,
                      confidences: list[float] | None = None) -> dict[str, Any] | None:
        """Conservatively correct a low-confidence four-character idiom."""
        if not re.fullmatch(r"[\u3400-\u9fff]{4}", word):
            return None
        self._load()
        idioms = self._idioms or {}
        if not idioms or word in idioms:
            return None
        confidence_values = ([float(value) for value in confidences]
                             if confidences and len(confidences) == 4 else [1.0] * 4)

        def equality_pattern(text: str) -> tuple[int, ...]:
            groups: dict[str, int] = {}
            return tuple(groups.setdefault(char, len(groups)) for char in text)

        candidates = []
        source_pattern = equality_pattern(word)
        for candidate, frequency in idioms.items():
            changed = [index for index in range(4) if candidate[index] != word[index]]
            distance = len(changed)
            if distance == 0 or distance > 2:
                continue
            changed_confidences = [confidence_values[index] for index in changed]
            if distance == 1:
                if changed_confidences[0] > 0.82:
                    continue
            else:
                # Two-character correction is only safe when both recognitions
                # are weak and the repeated-character structure is preserved.
                if max(changed_confidences) > 0.72 or equality_pattern(candidate) != source_pattern:
                    continue
            candidates.append({
                "text": candidate,
                "distance": distance,
                "frequency": frequency,
                "changed": changed,
                "changedConfidences": changed_confidences,
            })
        if not candidates:
            return None
        minimum_distance = min(candidate["distance"] for candidate in candidates)
        candidates = [candidate for candidate in candidates
                      if candidate["distance"] == minimum_distance]
        candidates.sort(key=lambda candidate: candidate["frequency"], reverse=True)
        best = candidates[0]
        if len(candidates) > 1 and best["frequency"] < candidates[1]["frequency"] * 1.5:
            return None
        return {
            "text": best["text"],
            "originalText": word,
            "method": "same-length-idiom",
            "distance": best["distance"],
            "changedIndices": best["changed"],
            "changedConfidences": [round(value, 4) for value in best["changedConfidences"]],
        }


@dataclass(frozen=True)
class OcrLine:
    text: str
    confidence: float
    box: list[list[float]]
    # Optional character-level boxes, ordered by CJK characters in ``text``.
    # They are produced by the ONNX CTC time-step mapper and are not required
    # for callers that only have the legacy line-level OCR result.
    char_boxes: list[list[list[float]]] | None = None
    char_spans: list[dict[str, Any]] | None = None

    @property
    def left(self) -> float:
        return min(point[0] for point in self.box)

    @property
    def top(self) -> float:
        return min(point[1] for point in self.box)

    @property
    def width(self) -> float:
        return max(point[0] for point in self.box) - self.left

    @property
    def height(self) -> float:
        return max(point[1] for point in self.box) - self.top


def _normalise_box(box: Any) -> list[list[float]]:
    if box is None:
        return [[0.0, 0.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]]
    return [[float(p[0]), float(p[1])] for p in box]


def _normalise_char_boxes(boxes: Any) -> list[list[list[float]]] | None:
    if not boxes:
        return None
    try:
        return [_normalise_box(box) for box in boxes]
    except (TypeError, ValueError, IndexError):
        return None


def _normalise_char_spans(spans: Any) -> list[dict[str, Any]] | None:
    if not isinstance(spans, (list, tuple)) or not spans:
        return None
    result = []
    try:
        for span in spans:
            if not isinstance(span, dict):
                return None
            start = float(span["start"])
            end = float(span["end"])
            col_num = max(float(span["col_num"]), 1.0)
            if end <= start:
                return None
            result.append({
                "char": str(span.get("char", "")),
                "start": start,
                "end": end,
                "center": float(span.get("center", (start + end - 1.0) / 2.0)),
                "col_num": col_num,
                "confidence": float(span.get("confidence", 0.0)),
            })
    except (TypeError, ValueError, KeyError):
        return None
    return result


def _json_safe(value: Any) -> Any:
    """Convert model containers to JSON without changing their values/order."""
    if hasattr(value, "tolist"):
        return _json_safe(value.tolist())
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _split_box(line: OcrLine, start: int, end: int, total: int) -> list[list[float]]:
    """Estimate a token box by slicing a recognised line horizontally."""
    if total <= 0:
        return line.box
    x0 = line.left + line.width * start / total
    x1 = line.left + line.width * end / total
    y0 = line.top
    y1 = line.top + line.height
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _slice_quad_box(box: list[list[float]], start: float, end: float) -> list[list[float]]:
    """Slice a four-point text polygon along its reading direction."""
    if len(box) < 4:
        return [[0.0, 0.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]]
    start = min(max(float(start), 0.0), 1.0)
    end = min(max(float(end), start), 1.0)
    p0, p1, p2, p3 = box[:4]

    def lerp(left: list[float], right: list[float], amount: float) -> list[float]:
        return [left[i] + (right[i] - left[i]) * amount for i in range(2)]

    top_start = lerp(p0, p1, start)
    top_end = lerp(p0, p1, end)
    bottom_start = lerp(p3, p2, start)
    bottom_end = lerp(p3, p2, end)
    return [top_start, top_end, bottom_end, bottom_start]


def _char_boxes_from_word_info(line: OcrLine, word_info: Any) -> list[list[list[float]]] | None:
    """Map CTC character time steps back onto the original line polygon."""
    if not word_info or not isinstance(word_info, (list, tuple)) or len(word_info) < 3:
        return None
    try:
        col_num = max(int(word_info[0]), 1)
        word_list = word_info[1]
        word_col_list = word_info[2]
    except (TypeError, ValueError, IndexError):
        return None

    columns: list[int] = []
    for chars, cols in zip(word_list or [], word_col_list or []):
        if isinstance(chars, str):
            chars = list(chars)
        if not isinstance(cols, (list, tuple)):
            continue
        for char, col in zip(chars, cols):
            if _CJK_RE.fullmatch(str(char)):
                try:
                    columns.append(int(col))
                except (TypeError, ValueError):
                    return None

    raw_cjk_count = len(_CJK_RE.findall(line.text))
    cleaned_cjk_count = len(_CJK_RE.findall(_LESSON_PREFIX_RE.sub("", line.text)))
    # ``第1课`` can contain CJK characters that the parser deliberately
    # removes. Keep only the columns belonging to the content that remains.
    if len(columns) == raw_cjk_count and cleaned_cjk_count < raw_cjk_count:
        columns = columns[raw_cjk_count - cleaned_cjk_count:]
    expected = cleaned_cjk_count
    if not columns or len(columns) != expected:
        return None
    positions = [min(max(column / col_num, 0.0), 1.0) for column in columns]
    if any(right < left for left, right in zip(positions, positions[1:])):
        return None

    boxes = []
    for index, position in enumerate(positions):
        start = 0.0 if index == 0 else (positions[index - 1] + position) / 2.0
        end = 1.0 if index == len(positions) - 1 else (position + positions[index + 1]) / 2.0
        boxes.append(_slice_quad_box(line.box, start, end))
    return boxes


def _cjk_spans_for_line(line: OcrLine, spans: Any) -> list[dict[str, Any]] | None:
    """Align raw decoded CTC spans with the CJK content kept by the parser."""
    normalised = _normalise_char_spans(spans)
    if not normalised:
        return None
    cjk_spans = [span for span in normalised if _CJK_RE.fullmatch(span["char"])]
    raw_count = len(_CJK_RE.findall(line.text))
    cleaned_count = len(_CJK_RE.findall(_LESSON_PREFIX_RE.sub("", line.text)))
    if len(cjk_spans) == raw_count and cleaned_count < raw_count:
        cjk_spans = cjk_spans[raw_count - cleaned_count:]
    if len(cjk_spans) != cleaned_count:
        return None
    if any(right["center"] < left["center"] for left, right in zip(cjk_spans, cjk_spans[1:])):
        return None
    return cjk_spans


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return float((ordered[middle - 1] + ordered[middle]) / 2.0)


def _char_boxes_from_spans(line: OcrLine,
                            spans: list[dict[str, Any]] | None) -> list[list[list[float]]] | None:
    """Build non-touching character boxes around the model's CTC activations."""
    if not spans:
        return None
    centers = [float(span["center"]) for span in spans]
    pitches = [right - left for left, right in zip(centers, centers[1:]) if right > left]
    typical_pitch = _median(pitches) if pitches else max(float(spans[0]["end"]) - float(spans[0]["start"]), 1.0)
    boxes = []
    for index, span in enumerate(spans):
        col_num = max(float(span["col_num"]), 1.0)
        center = float(span["center"])
        run_half = max((float(span["end"]) - float(span["start"])) / 2.0, 0.5)
        half_width = max(run_half, typical_pitch * 0.36)
        if index:
            half_width = min(half_width, max((center - centers[index - 1]) * 0.46, 0.5))
        if index + 1 < len(centers):
            half_width = min(half_width, max((centers[index + 1] - center) * 0.46, 0.5))
        start = max(0.0, center - half_width) / col_num
        end = min(col_num, center + half_width) / col_num
        boxes.append(_slice_quad_box(line.box, start, end))
    return boxes


def _merge_char_boxes(boxes: list[list[list[float]]]) -> list[list[float]] | None:
    """Merge contiguous character quads while preserving line slant."""
    if not boxes:
        return None
    if len(boxes) == 1:
        return boxes[0]
    return [boxes[0][0], boxes[-1][1], boxes[-1][2], boxes[0][3]]


def _char_gap(left: list[list[float]], right: list[list[float]]) -> float:
    """Approximate the horizontal gap between two adjacent character boxes."""
    left_edge = (left[1][0] + left[2][0]) / 2.0
    right_edge = (right[0][0] + right[3][0]) / 2.0
    return max(0.0, right_edge - left_edge)


def _visual_spans(indices: list[int], char_boxes: list[list[list[float]]] | None,
                  char_spans: list[dict[str, Any]] | None = None) -> list[tuple[int, int]]:
    """Split a CJK run using CTC blank/pitch evidence, then box gaps as fallback."""
    if len(indices) >= 3 and char_spans and max(indices) < len(char_spans):
        pairs = list(zip(indices, indices[1:]))
        pitches = [
            float(char_spans[right]["center"]) - float(char_spans[left]["center"])
            for left, right in pairs
        ]
        blank_gaps = [
            max(0.0, float(char_spans[right]["start"]) - float(char_spans[left]["end"]))
            for left, right in pairs
        ]
        # Estimate ordinary within-word spacing from the lower 60%; vocabulary
        # rows often contain nearly as many word gaps as within-word gaps.
        baseline_count = max(1, math.ceil(len(pitches) * 0.6))
        typical_pitch = _median(sorted(pitches)[:baseline_count])
        typical_blank = _median(sorted(blank_gaps)[:baseline_count])
        if typical_pitch > 0:
            spans: list[tuple[int, int]] = []
            start = indices[0]
            for pair_index, (previous, current) in enumerate(pairs):
                pitch = pitches[pair_index]
                blank = blank_gaps[pair_index]
                pitch_break = pitch >= typical_pitch * 1.38 and pitch - typical_pitch >= 0.75
                blank_break = blank >= max(typical_blank + 1.0, typical_pitch * 0.18, 1.0)
                very_large_pitch = pitch >= typical_pitch * 1.72
                if (pitch_break and blank_break) or very_large_pitch:
                    spans.append((start, previous + 1))
                    start = current
            spans.append((start, indices[-1] + 1))
            # CTC spans are the primary evidence. Even when they find no word
            # boundary, do not let the older estimated-box fallback invent one.
            return spans

    if len(indices) < 2 or not char_boxes or max(indices) >= len(char_boxes):
        return [(indices[0], indices[-1] + 1)] if indices else []
    widths = [
        max(1.0, max(point[0] for point in char_boxes[index]) -
            min(point[0] for point in char_boxes[index]))
        for index in indices
    ]
    threshold = max(3.0, (sorted(widths)[len(widths) // 2]) * 0.28)
    spans: list[tuple[int, int]] = []
    start = indices[0]
    for previous, current in zip(indices, indices[1:]):
        if _char_gap(char_boxes[previous], char_boxes[current]) > threshold:
            spans.append((start, previous + 1))
            start = current
    spans.append((start, indices[-1] + 1))
    return spans


def _word_item(text: str, lang: str, kind: str, line: OcrLine,
               box: list[list[float]], index: int) -> dict[str, Any]:
    return {
        "id": f"ocr-{index + 1}",
        "text": text,
        "lang": lang,
        "kind": kind,
        "source": "photo",
        "ocr": {
            "rawText": line.text,
            "confidence": round(line.confidence, 4),
            "box": box,
        },
        "selected": True,
    }


def _reading_order(lines: Iterable[OcrLine]) -> list[OcrLine]:
    """Stable low-cost reading order; column segmentation is mode-specific."""
    return sorted(lines, key=lambda line: (round(line.top / max(line.height, 1)), line.left))


def _extract_zh_chars(lines: list[OcrLine]) -> list[dict[str, Any]]:
    result = []
    for line in _reading_order(lines):
        cleaned = _LESSON_PREFIX_RE.sub("", line.text)
        matches = list(_CJK_RE.finditer(cleaned))
        for cjk_index, match in enumerate(matches):
            box = (line.char_boxes[cjk_index]
                   if line.char_boxes and cjk_index < len(line.char_boxes)
                   else _split_box(line, match.start(), match.end(), max(len(cleaned), 1)))
            result.append(_word_item(
                match.group(), "zh-CN", "char", line,
                box,
                len(result),
            ))
    return result


def _extract_zh_words(lines: list[OcrLine],
                      segmenter: Callable[[str], list[str]] | None = None,
                      word_checker: Callable[[str], bool] | None = None,
                      word_corrector: Callable[[str, list[float] | None], dict[str, Any] | None] | None = None
                      ) -> tuple[list[dict[str, Any]], bool]:
    result = []
    ambiguous = False
    for line in _reading_order(lines):
        cleaned = _LESSON_PREFIX_RE.sub("", line.text).strip()
        all_cjk = list(_CJK_RE.findall(cleaned))
        cjk_positions = [match.start() for match in _CJK_RE.finditer(cleaned)]
        has_char_boxes = bool(line.char_boxes and len(line.char_boxes) == len(all_cjk))
        char_boxes = line.char_boxes if has_char_boxes else None
        # Preserve OCR whitespace as the strongest word-boundary evidence.
        entries: list[dict[str, Any]] = []
        for match in re.finditer(r"[^\s,，、;；。|]+", cleaned):
            indices = [index for index, position in enumerate(cjk_positions)
                       if match.start() <= position < match.end()]
            if not indices:
                continue
            for span_start, span_end in _visual_spans(indices, char_boxes, line.char_spans):
                cjk = "".join(all_cjk[span_start:span_end])
                if len(cjk) > 4:
                    if segmenter:
                        at = span_start
                        segmented = segmenter(cjk)
                        for word in segmented:
                            length = len("".join(_CJK_RE.findall(word)))
                            if length:
                                entries.append({"text": word, "indices": list(range(at, at + length))})
                                at += length
                    else:
                        ambiguous = True
                        entries.append({"text": cjk, "indices": list(range(span_start, span_end))})
                else:
                    entries.append({"text": cjk, "indices": list(range(span_start, span_end))})

        if word_checker and len(entries) > 1:
            merged = []
            at = 0
            while at < len(entries):
                left = entries[at]["text"]
                if at + 1 < len(entries):
                    right = entries[at + 1]["text"]
                    pair = left + right
                    if len(left) == len(right) == 1 and word_checker(pair):
                        merged.append({
                            "text": pair,
                            "indices": entries[at]["indices"] + entries[at + 1]["indices"],
                        })
                        at += 2
                        continue
                merged.append(entries[at])
                at += 1
            entries = merged

        for entry in entries:
            word = "".join(_CJK_RE.findall(entry["text"]))
            if not word:
                continue
            indices = entry["indices"]
            char_confidences = None
            if line.char_spans and indices and max(indices) < len(line.char_spans):
                char_confidences = [float(line.char_spans[index].get("confidence", 0.0))
                                    for index in indices]
            correction = word_corrector(word, char_confidences) if word_corrector else None
            output_word = str(correction.get("text", word)) if correction else word
            box = (_merge_char_boxes([char_boxes[index] for index in indices])
                   if char_boxes and indices and max(indices) < len(char_boxes)
                   else None)
            if box is None:
                # Fall back to the source-string span when character boxes
                # were not available or a segmentation result was unusual.
                start = cjk_positions[indices[0]] if indices and indices[0] < len(cjk_positions) else 0
                end = (cjk_positions[indices[-1]] + 1
                       if indices and indices[-1] < len(cjk_positions) else start + len(word))
                box = _split_box(line, start, end, max(len(cleaned), 1))
            item = _word_item(
                output_word, "zh-CN", "word", line,
                box,
                len(result),
            )
            if correction:
                item["ocr"]["originalText"] = word
                item["ocr"]["correction"] = {
                    key: value for key, value in correction.items() if key != "text"
                }
            result.append(item)
    return result, ambiguous


def _english_columns(lines: list[OcrLine]) -> list[OcrLine]:
    """Order common two-column vocabulary pages without heavy layout models."""
    if len(lines) < 4:
        return _reading_order(lines)
    lefts = sorted(line.left for line in lines)
    gaps = [(lefts[i + 1] - lefts[i], i) for i in range(len(lefts) - 1)]
    gap, at = max(gaps, default=(0, 0))
    threshold = lefts[at] + gap / 2
    page_span = max(lefts) - min(lefts)
    if gap < max(page_span * 0.22, 20):
        return _reading_order(lines)
    left = sorted((line for line in lines if line.left < threshold), key=lambda x: (x.top, x.left))
    right = sorted((line for line in lines if line.left >= threshold), key=lambda x: (x.top, x.left))
    return left + right


def _extract_en_vocab(lines: list[OcrLine]) -> list[dict[str, Any]]:
    result = []
    for line in _english_columns(lines):
        raw = _PAGE_RE.sub("", line.text).strip()
        if not raw or _UNIT_RE.match(raw):
            continue
        # Text before IPA is the headword. Without an IPA slash, stop at CJK.
        before_ipa = raw.split("/", 1)[0]
        before_ipa = re.split(r"[\u3400-\u9fff]", before_ipa, maxsplit=1)[0]
        before_ipa = re.sub(r"^\s*\d+[.、)]?\s*", "", before_ipa).strip(" .,:;-")
        match = _EN_ENTRY_RE.fullmatch(before_ipa)
        if not match:
            continue
        text = re.sub(r"\s+", " ", match.group()).replace("’", "'")
        result.append(_word_item(
            text, "en-US", "phrase" if " " in text else "word", line,
            _split_box(line, 0, len(before_ipa), max(len(raw), 1)), len(result),
        ))
    return result


def extract_from_ocr_lines(raw_lines: Iterable[dict[str, Any]], mode: str,
                           low_confidence: float = 0.72,
                           segmenter: Callable[[str], list[str]] | None = None,
                           word_checker: Callable[[str], bool] | None = None,
                           word_corrector: Callable[[str, list[float] | None], dict[str, Any] | None] | None = None
                           ) -> dict[str, Any]:
    """Convert OCR line results to the stable frontend contract."""
    if mode not in SUPPORTED_MODES:
        raise ValueError(f"unsupported mode: {mode}")
    lines = [OcrLine(
        text=str(row.get("text", "")).strip(),
        confidence=float(row.get("confidence", row.get("score", 0.0))),
        box=_normalise_box(row.get("box")),
        char_boxes=_normalise_char_boxes(row.get("charBoxes", row.get("char_boxes"))),
        char_spans=_normalise_char_spans(row.get("charSpans", row.get("char_spans"))),
    ) for row in raw_lines if str(row.get("text", "")).strip()]

    warnings = []
    if mode == "zh":
        page_text = "".join(line.text for line in lines)
        body_lines = [line for line in lines
                      if "写字表" not in line.text and "词语表" not in line.text]
        content_lines = ["".join(_CJK_RE.findall(line.text)) for line in body_lines
                         if "写字表" not in line.text and "词语表" not in line.text]
        short_ratio = (sum(1 for text in content_lines if len(text) <= 1) /
                       max(len(content_lines), 1))
        detected_mode = "zh_char" if "写字表" in page_text or (
            "词语表" not in page_text and short_ratio >= 0.6
        ) else "zh_word"
        if detected_mode == "zh_char":
            items = _extract_zh_chars(body_lines)
        else:
            items, ambiguous = _extract_zh_words(
                body_lines, segmenter, word_checker, word_corrector
            )
            if ambiguous:
                warnings.append("部分词语缺少清晰空格：请在确认页检查合并或拆分，或缩小取景框后重新拍摄少量词语。")
        warnings.append("已自动按%s提取。" % ("单字" if detected_mode == "zh_char" else "词语"))
    elif mode == "zh_char":
        items = _extract_zh_chars(lines)
    elif mode == "zh_word":
        items, ambiguous = _extract_zh_words(lines, segmenter, word_checker, word_corrector)
        if ambiguous:
            warnings.append("部分词语缺少清晰空格：请在确认页检查合并或拆分，或缩小取景框后重新拍摄少量词语。")
    else:
        items = _extract_en_vocab(lines)

    correction_count = sum(bool(item.get("ocr", {}).get("correction")) for item in items)
    if correction_count:
        warnings.append(f"已根据同长度成语词典纠正 {correction_count} 处，请在确认页核对。")
    low_count = sum(item["ocr"]["confidence"] < low_confidence for item in items)
    if low_count:
        warnings.append(f"有 {low_count} 个低置信度结果，请重点核对。")
    if not items:
        warnings.append("没有提取到可听写内容，请调整裁剪范围或重新拍摄。")
    return {"items": items, "warnings": warnings}


class DictationOcr:
    """Lazy wrapper around tip-word's existing ONNX detector/recognizer.

    Models are loaded on first request and shared behind a lock to cap memory
    and avoid concurrent DirectML/CPU inference spikes.
    """

    def __init__(self, tip_word_root: str | Path, max_input_side: int = 1800,
                 max_text_boxes: int = 100,
                 jieba_dict_path: str | Path | None = None):
        self.tip_word_root = Path(tip_word_root).resolve()
        self.max_input_side = max(960, int(max_input_side))
        self.max_text_boxes = max(1, int(max_text_boxes))
        self._detector = None
        self._recognizer = None
        self._lock = threading.Lock()
        self._provider = "not-loaded"
        self._segmenter = ChineseLexiconSegmenter(jieba_dict_path)

    def _load(self) -> None:
        if self._detector is not None:
            return
        root_text = str(self.tip_word_root)
        if root_text not in sys.path:
            sys.path.insert(0, root_text)
        module_path = self.tip_word_root / "core" / "inference.py"
        spec = importlib.util.spec_from_file_location("tip_word_inference", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load OCR adapter: {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = __import__("configs.config", fromlist=["*"])
        detector = module.TextDetector(
            str(self.tip_word_root / config.PADDLEX_DET_DIR),
            thresh=config.DET_THRESH,
            backend=config.OCR_DET_BACKEND,
        )
        recognizer = module.TextRecognizer(
            str(self.tip_word_root / config.PADDLEX_REC_DIR),
            backend=config.OCR_REC_BACKEND,
        )
        crop = module.get_rotate_crop_image
        detector_session = getattr(detector, "_session", None)
        recognizer_session = getattr(recognizer, "_session", None)
        providers = []
        for session in (detector_session, recognizer_session):
            if session is not None and hasattr(session, "get_providers"):
                providers.extend(session.get_providers())
        self._provider = ",".join(dict.fromkeys(providers)) or str(
            getattr(config, "ONNX_EXECUTION_PROVIDER", "unknown")
        )
        self._detector = detector
        self._recognizer = recognizer
        self._crop = crop

    def _prepare_image(self, image: Any) -> tuple[Any, bool]:
        """Cap decoded camera images before perspective crops allocate memory.

        The detector already resizes its own tensor to a 960px longest edge, so
        retaining an 8K source cannot improve detection.  An 1800px source still
        leaves ample resolution for the 48px-high recognition input.
        """
        shape = getattr(image, "shape", None)
        if not shape or len(shape) < 2:
            return image, False
        height, width = int(shape[0]), int(shape[1])
        longest = max(height, width)
        if longest <= self.max_input_side:
            return image, False
        import cv2
        ratio = self.max_input_side / float(longest)
        resized = cv2.resize(
            image,
            (max(1, round(width * ratio)), max(1, round(height * ratio))),
            interpolation=cv2.INTER_AREA,
        )
        return resized, True

    def _enhance_image(self, image: Any) -> Any:
        """Apply a lightweight document-scan enhancement before OCR.

        CLAHE improves uneven book lighting while preserving coloured textbook
        text; a small unsharp mask restores strokes softened by the camera.
        This deliberately avoids hard thresholding, which can erase blue/grey
        glyphs and punctuation.
        """
        import cv2
        if getattr(image, "ndim", 0) != 3 or image.shape[2] < 3:
            return image
        lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
        light, a_channel, b_channel = cv2.split(lab)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        light = clahe.apply(light)
        enhanced = cv2.cvtColor(cv2.merge((light, a_channel, b_channel)), cv2.COLOR_LAB2BGR)
        blurred = cv2.GaussianBlur(enhanced, (0, 0), 1.0)
        return cv2.addWeighted(enhanced, 1.18, blurred, -0.18, 0)

    def warmup(self) -> dict[str, Any]:
        """Load the shared models without running inference.

        The photo screen can call this in the background while the user frames
        the page, hiding most cold-start latency without loading models at app
        startup for users who never open dictation.
        """
        started = time.perf_counter()
        with self._lock:
            cold_start = self._detector is None
            self._load()
        return {
            "ready": True,
            "coldStart": cold_start,
            "provider": self._provider,
            "loadMs": round((time.perf_counter() - started) * 1000, 1),
        }

    def extract(self, image: Any, mode: str, progress=None) -> dict[str, Any]:
        """Extract dictation items and optionally report coarse real stages.

        ``progress`` receives ``(phase, percent, message, details)``.  The
        recognizer is one blocking ONNX call, so percentages deliberately stay
        coarse instead of pretending to know per-character completion.
        """
        def report(phase: str, percent: int, message: str, **details) -> None:
            if progress is not None:
                progress(phase, percent, message, details)

        if mode not in SUPPORTED_MODES:
            raise ValueError(f"unsupported mode: {mode}")
        total_started = time.perf_counter()
        report("preparing", 8, "正在整理照片")
        source_shape = getattr(image, "shape", ())
        image, resized = self._prepare_image(image)
        image = self._enhance_image(image)
        prepared_shape = getattr(image, "shape", ())
        prepare_done = time.perf_counter()
        report("queued", 12, "正在等待 OCR 处理")
        with self._lock:
            lock_acquired = time.perf_counter()
            cold_start = self._detector is None
            report("loading", 18, "首次使用，正在加载识别模型" if cold_start else "识别模型已就绪")
            self._load()
            load_done = time.perf_counter()
            report("detecting", 28, "正在定位文字区域")
            boxes = self._detector.predict(image)
            detect_done = time.perf_counter()
            boxes_truncated = max(0, len(boxes) - self.max_text_boxes)
            if boxes_truncated:
                boxes = sorted(
                    boxes,
                    key=lambda box: (
                        min(float(point[1]) for point in box),
                        min(float(point[0]) for point in box),
                    ),
                )[:self.max_text_boxes]
            report("cropping", 38, f"发现 {len(boxes)} 个文字区域，正在校正方向", textBoxes=len(boxes))
            crops = [self._crop(image, box) for box in boxes]
            crop_done = time.perf_counter()
            if not crops:
                payload = extract_from_ocr_lines(
                    [], mode, segmenter=self._segmenter.segment,
                    word_checker=self._segmenter.contains,
                    word_corrector=self._segmenter.correct_idiom,
                )
                recognise_done = crop_done
                result = None
            else:
                report("recognizing", 45, f"正在识别 {len(crops)} 个文字区域，这一步通常最久", textBoxes=len(crops))
                # Character boxes are useful for Chinese word-boundary
                # grouping. Keep the established English vocabulary path
                # unchanged and avoid requesting extra metadata there.
                need_char_boxes = mode in {"zh", "zh_char", "zh_word"}
                result = self._recognizer.predict(crops, return_word_box=need_char_boxes)
                recognise_done = time.perf_counter()
        # Keep the recognizer output before any text cleanup/unpacking. This
        # is the audit copy; parsed items below are a separate product layer.
        raw_rec_text = result.get("rec_text", []) if result else []
        raw_rec_score = result.get("rec_score", []) if result else []
        raw_rec_char_spans = result.get("rec_char_spans", []) if result else []
        texts = raw_rec_text
        scores = raw_rec_score
        if isinstance(texts, str):
            texts = [texts]
        elif isinstance(texts, tuple) and len(texts) >= 2 and isinstance(texts[0], str):
            texts = [texts]
        if isinstance(scores, (int, float)):
            scores = [scores]
        if len(texts) == 1 and isinstance(raw_rec_char_spans, (list, tuple)):
            if not raw_rec_char_spans or isinstance(raw_rec_char_spans[0], dict):
                span_rows = [raw_rec_char_spans]
            else:
                span_rows = list(raw_rec_char_spans)
        elif isinstance(raw_rec_char_spans, (list, tuple)):
            span_rows = list(raw_rec_char_spans)
        else:
            span_rows = []
        recognised_texts = []
        raw_lines = []
        for index, recognised in enumerate(texts):
            if index >= len(boxes):
                continue
            word_info = None
            text = recognised
            if isinstance(recognised, (tuple, list)) and len(recognised) >= 2:
                text, word_info = recognised[0], recognised[1]
            text = str(text)
            score = scores[index] if index < len(scores) else 0.0
            line_box = boxes[index].tolist() if hasattr(boxes[index], "tolist") else boxes[index]
            line = OcrLine(text=text.strip(), confidence=float(score), box=_normalise_box(line_box))
            raw_spans = span_rows[index] if index < len(span_rows) else None
            char_spans = _cjk_spans_for_line(line, raw_spans)
            char_boxes = (_char_boxes_from_spans(line, char_spans) or
                          _char_boxes_from_word_info(line, word_info))
            recognised_texts.append(text)
            raw_lines.append({
                "text": text,
                "confidence": score,
                "box": line_box,
                "charBoxes": char_boxes,
                "charSpans": char_spans,
            })
        report("parsing", 90, "正在按词典拆分并整理词表", textBoxes=len(boxes))
        payload = extract_from_ocr_lines(
            raw_lines, mode, segmenter=self._segmenter.segment,
            word_checker=self._segmenter.contains,
            word_corrector=self._segmenter.correct_idiom,
        )
        # Keep a JSON-safe PaddleOCR-compatible raw layer alongside the
        # product-level parsed words.  This makes model errors auditable
        # without losing the original detector/recognizer output.
        serial_boxes = [
            box.tolist() if hasattr(box, "tolist") else box for box in boxes
        ]
        payload["paddleocrRaw"] = {
            # Exact adapter-level output, converted only from NumPy/tuples to
            # JSON containers. No stripping, segmentation, or deduplication.
            "raw": {
                "dt_polys": _json_safe(serial_boxes),
                "rec_text": _json_safe(raw_rec_text),
                "rec_score": _json_safe(raw_rec_score),
                "rec_char_spans": _json_safe(raw_rec_char_spans),
            },
            "dt_polys": serial_boxes,
            "rec_texts": recognised_texts,
            "rec_scores": [float(score) for score in scores],
            "char_boxes": [row.get("charBoxes") for row in raw_lines],
            "char_spans": [row.get("charSpans") for row in raw_lines],
            "rec_boxes": [
                [
                    min(float(point[0]) for point in box),
                    min(float(point[1]) for point in box),
                    max(float(point[0]) for point in box),
                    max(float(point[1]) for point in box),
                ] for box in serial_boxes
            ],
            "legacy": [
                [box, [recognised_texts[index], float(scores[index])]]
                for index, box in enumerate(serial_boxes)
                if index < len(recognised_texts) and index < len(scores)
            ],
        }
        if boxes_truncated:
            payload["warnings"].append(
                f"文字区域过多，仅处理前 {self.max_text_boxes} 个，请缩小到词表区域后重拍。"
            )
        parsed_done = time.perf_counter()
        def ms(start: float, end: float) -> float:
            return round((end - start) * 1000, 1)
        payload["timing"] = {
            "coldStart": cold_start,
            "provider": self._provider,
            "sourceSize": list(source_shape[:2][::-1]) if len(source_shape) >= 2 else None,
            "processedSize": list(prepared_shape[:2][::-1]) if len(prepared_shape) >= 2 else None,
            "sourceResized": resized,
            "textBoxes": len(boxes),
            "queueMs": ms(prepare_done, lock_acquired),
            "prepareMs": ms(total_started, prepare_done),
            "loadMs": ms(lock_acquired, load_done),
            "detectMs": ms(load_done, detect_done),
            "cropMs": ms(detect_done, crop_done),
            "recognizeMs": ms(crop_done, recognise_done),
            "parseMs": ms(recognise_done, parsed_done),
            "totalMs": ms(total_started, parsed_done),
        }
        report("completed", 100, f"识别完成，共整理出 {len(payload['items'])} 个内容", itemCount=len(payload["items"]))
        return payload
