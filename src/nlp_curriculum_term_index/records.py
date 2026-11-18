"""课案文本记录。

记录保留原始正文，同时保存一份规范化文本用于分词和术语定位；检索结果引用
规范化文本中的位置，并同时给出原始字段、来源和修订号，方便回看命中原文。
"""

from dataclasses import dataclass, asdict
from datetime import datetime


def _first(value, names, default=""):
    for name in names:
        if name in value and value[name] is not None:
            return value[name]
    return default


@dataclass(frozen=True)
class TextRecord:
    record_id: str
    source: str
    revision: int
    observed_at: str
    text: str
    context: str = ""
    subject: str = ""
    textbook_version: str = ""
    original_text: str = ""

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise TypeError("文本记录必须是对象")
        raw_text = str(value.get("text", ""))
        original_text = str(
            _first(value, ("original_text", "source_text", "raw_text"), raw_text)
        )
        record = cls(
            record_id=str(value.get("record_id", "")).strip(),
            source=str(value.get("source", "")).strip(),
            revision=int(value.get("revision", 0)),
            observed_at=str(value.get("observed_at", "")).strip(),
            text=raw_text,
            context=str(value.get("context", "")).strip(),
            subject=str(
                _first(value, ("subject", "discipline", "学科"), "")
            ).strip(),
            textbook_version=str(
                _first(
                    value,
                    ("textbook_version", "textbook_edition", "edition", "教材版本"),
                    "",
                )
            ).strip(),
            original_text=original_text if original_text.strip() else raw_text,
        )
        if not record.record_id or not record.source or record.revision < 1 or not record.text.strip():
            raise ValueError("文本记录缺少身份、来源、有效修订号或正文")
        datetime.fromisoformat(record.observed_at.replace("Z", "+00:00"))
        return record

    def to_dict(self):
        return asdict(self)
