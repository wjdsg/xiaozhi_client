"""Read-only textbook catalog API.

The source JSON is loaded once on first use. Responses are deliberately split
by hierarchy so the lamp never has to transfer the entire catalog to render one
selection screen.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

# Web 框架适配已迁到 web.py；本文件只保留可复用的目录数据模型。


SUBJECT_META = {
    "chinese": {
        "name": "语文",
        "publisher": "人民教育出版社",
        "edition": "未标注出版年份",
        "version": "部编版",
        "stage": "小学",
        "declaredEntryCount": 3198,
    },
    "english": {
        "name": "英语",
        "publisher": "人民教育出版社",
        "edition": "未标注出版年份",
        "version": "PEP",
        "stage": "小学",
        "declaredEntryCount": 952,
    },
}

_GRADE_NUMBERS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
                  "七": 7, "八": 8, "九": 9}


def _apply_structured_chinese_volume(data_dir: Path, textbook: dict) -> None:
    """Overlay the audited Grade 4 upper-volume list without changing legacy grades."""
    path = Path(data_dir) / "chinese_g4_up_structured.json"
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        source = json.load(handle)
    lessons = {item["lesson"]: item for item in source["lessons"]}
    units = []
    for source_unit in source["units"]:
        unit_lessons = []
        for lesson_number in source_unit["lessons"]:
            lesson = lessons[lesson_number]
            unit_lessons.append({
                "lesson": lesson_number,
                "title": lesson["title"],
                "words": lesson.get("words", []),
                "characters": lesson.get("characters", []),
                "structured": True,
            })
        units.append({
            "unit": source_unit["unit"],
            "title": source_unit["title"],
            "lessons": unit_lessons,
            "structured": True,
        })
    for grade in textbook.get("grades", []):
        if grade.get("grade") == source["grade"]:
            grade["units"] = units
            grade["structured"] = True
            return


def _parse_volume(label: str) -> tuple[int, str]:
    match = re.fullmatch(r"([一二三四五六七八九])年级([上下])册", label)
    if not match:
        raise ValueError(f"unsupported grade label: {label}")
    return _GRADE_NUMBERS[match.group(1)], f"{match.group(2)}册"


def _volume_id(subject: str, grade: int, volume: str) -> str:
    side = "up" if volume == "上册" else "down"
    return f"{subject}-primary-g{grade}-{side}"


class CatalogStore:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self._lock = threading.Lock()
        self._data = None
        self._volumes = None

    def _ensure_loaded(self):
        if self._data is not None:
            return
        with self._lock:
            if self._data is not None:
                return
            raw = {}
            for subject in SUBJECT_META:
                path = self.data_dir / f"{subject}_textbook.json"
                with path.open("r", encoding="utf-8") as handle:
                    raw[subject] = json.load(handle)
            _apply_structured_chinese_volume(self.data_dir, raw["chinese"])
            volumes = {}
            for subject, textbook in raw.items():
                for index, grade_data in enumerate(textbook["grades"]):
                    grade, volume = _parse_volume(grade_data["grade"])
                    vid = _volume_id(subject, grade, volume)
                    volumes[vid] = (subject, index, grade, volume, grade_data)
            self._data = raw
            self._volumes = volumes

    def subjects(self):
        self._ensure_loaded()
        result = []
        for subject, meta in SUBJECT_META.items():
            volumes = [v for v in self._volumes.values() if v[0] == subject]
            entry_count = sum(self._entry_count(subject, v[4]) for v in volumes)
            result.append({"id": subject, **meta, "volumeCount": len(volumes),
                           "entryCount": entry_count})
        return result

    def volumes(self, subject: str):
        self._ensure_loaded()
        self._require_subject(subject)
        result = []
        for vid, (sid, _index, grade, volume, data) in self._volumes.items():
            if sid != subject:
                continue
            result.append({
                "id": vid, "subject": subject, **SUBJECT_META[subject],
                "grade": grade, "gradeLabel": f"{grade}年级", "volume": volume,
                "label": data["grade"], "unitCount": len(data["units"]),
                "entryCount": self._entry_count(subject, data),
                "structured": bool(data.get("structured")),
            })
        return result

    def units(self, subject: str, volume_id: str):
        _, _, _, _, volume = self._get_volume(subject, volume_id)
        result = []
        for index, unit in enumerate(volume["units"]):
            lessons = unit.get("lessons") if subject == "chinese" else [unit]
            count = (len(unit["vocabulary"])
                     if subject == "chinese" and unit.get("vocabulary") and not unit.get("structured")
                     else sum(len(self._lesson_values(item)) for item in lessons))
            result.append({"id": index, "unit": unit["unit"], "title": unit["title"],
                           "lessonCount": len(lessons), "entryCount": count,
                           "structured": bool(unit.get("structured"))})
        return result

    def lessons(self, subject: str, volume_id: str, unit_index: int):
        unit = self._get_unit(subject, volume_id, unit_index)
        lessons = unit.get("lessons") if subject == "chinese" else [unit]
        return [{"id": index, "lesson": lesson.get("lesson", unit["unit"]),
                 "title": lesson["title"], "entryCount": len(self._lesson_values(lesson)),
                 "structured": bool(lesson.get("structured"))}
                for index, lesson in enumerate(lessons)]

    def entries(self, subject: str, volume_id: str, unit_index: int, lesson_index: int):
        unit = self._get_unit(subject, volume_id, unit_index)
        lessons = unit.get("lessons") if subject == "chinese" else [unit]
        if lesson_index < 0 or lesson_index >= len(lessons):
            raise KeyError("lesson not found")
        lesson = lessons[lesson_index]
        if subject == "chinese":
            if "characters" in lesson:
                words = [{"id": f"word-{index}", "text": text, "language": "zh-CN",
                          "kind": "word", "source": "vocabulary_table"}
                         for index, text in enumerate(lesson.get("words", []))]
                characters = [{"id": f"char-{index}", "text": text, "language": "zh-CN",
                               "kind": "char", "source": "writing_table"}
                              for index, text in enumerate(lesson.get("characters", []))]
                return words + characters
            return [{"id": index, "text": text, "language": "zh-CN",
                     "kind": "char" if len(text) == 1 else "word"}
                    for index, text in enumerate(lesson["words"])]
        return [{"id": index, "text": item["word"], "meaning": item.get("chinese", ""),
                 "language": "en-US", "kind": "phrase" if " " in item["word"] else "word"}
                for index, item in enumerate(lesson["words"])]

    def unit_entries(self, subject: str, volume_id: str, unit_index: int):
        """Return the user-facing vocabulary for a unit."""
        unit = self._get_unit(subject, volume_id, unit_index)
        if subject == "chinese" and unit.get("structured"):
            entries = []
            for lesson_index, lesson in enumerate(unit.get("lessons", [])):
                for item in self.entries(subject, volume_id, unit_index, lesson_index):
                    entries.append({**item, "id": len(entries),
                                    "lesson": lesson.get("lesson"),
                                    "lessonTitle": lesson.get("title")})
            return entries
        if subject == "chinese" and unit.get("vocabulary"):
            return [{"id": index, "text": text, "language": "zh-CN",
                     "kind": "word" if len(text) > 1 else "char"}
                    for index, text in enumerate(unit["vocabulary"])]
        return self.entries(subject, volume_id, unit_index, 0)

    def stats(self):
        self._ensure_loaded()
        return {item["id"]: {"volumes": item["volumeCount"], "entries": item["entryCount"]}
                for item in self.subjects()}

    def _require_subject(self, subject):
        if subject not in SUBJECT_META:
            raise KeyError("subject not found")

    def _get_volume(self, subject, volume_id):
        self._ensure_loaded()
        self._require_subject(subject)
        found = self._volumes.get(volume_id)
        if found is None or found[0] != subject:
            raise KeyError("volume not found")
        return found

    def _get_unit(self, subject, volume_id, unit_index):
        volume = self._get_volume(subject, volume_id)[4]
        if unit_index < 0 or unit_index >= len(volume["units"]):
            raise KeyError("unit not found")
        return volume["units"][unit_index]

    @staticmethod
    def _entry_count(subject, volume):
        if subject == "english":
            return sum(len(unit["words"]) for unit in volume["units"])
        return sum(len(CatalogStore._lesson_values(lesson)) for unit in volume["units"]
                   for lesson in unit["lessons"])

    @staticmethod
    def _lesson_values(lesson):
        if "characters" in lesson:
            return list(lesson.get("words", [])) + list(lesson.get("characters", []))
        return list(lesson.get("words", []))
