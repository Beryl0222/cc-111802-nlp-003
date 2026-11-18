from .glossary import GlossaryIndex
from .records import TextRecord
from .text import annotate, normalize_text


def _first(value, names, default=None):
    for name in names:
        if name in value and value[name] is not None:
            return value[name]
    return default


class TextWorkflow:
    def __init__(self, repository, glossary=None):
        self.repository = repository
        self.glossary = glossary

    def _analyze(self, record):
        normalized = normalize_text(record["text"])
        record["text"] = normalized
        record["analysis"] = annotate(record)
        if self.glossary is not None:
            try:
                version = self.glossary.effective()
            except LookupError:
                version = None
            if version is not None:
                index = GlossaryIndex(version)
                record["term_mentions"] = index.recognize(
                    normalized,
                    subject=record.get("subject") or None,
                    textbook_version=record.get("textbook_version") or None,
                )
                record["term_analysis_glossary"] = version.summary()
        return record

    def ingest(self, values):
        if not isinstance(values, list):
            raise TypeError("导入内容必须是记录列表")
        incoming = [TextRecord.from_dict(value).to_dict() for value in values]
        existing = self.repository.read()
        current = {record["record_id"]: record for record in existing}
        for record in incoming:
            previous = current.get(record["record_id"])
            if previous is None or record["revision"] >= previous["revision"]:
                current[record["record_id"]] = self._analyze(record)
        result = sorted(current.values(), key=lambda row: row["record_id"])
        self.repository.replace(result)
        return {"received": len(incoming), "stored": len(result)}

    def list_records(self, source=None, subject=None, textbook_version=None):
        records = self.repository.read()
        if source:
            records = [record for record in records if record["source"] == source]
        if subject:
            records = [record for record in records if _first(record, ("subject",), "") == subject]
        if textbook_version:
            records = [
                record
                for record in records
                if _first(record, ("textbook_version",), "") == textbook_version
            ]
        return records
