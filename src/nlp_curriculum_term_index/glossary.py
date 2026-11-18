"""学科术语表：概念、词条与不可变的术语表版本。

设计约束（对应教研检索场景）：

* **概念是唯一归属**。词条只指向一个概念，学科由概念决定，词条不能自带一套
  与之冲突的学科，因此术语识别、教材版本过滤和检索结果不会各说各话。
* **同一词面可以指向不同学科的不同概念**。解析结果保持为候选集合，调用方
  必须显式给出学科或接受歧义，绝不生成“全局同义词”。
* **旧称与简称是词条属性**，可以限定教材版本；旧称不会因为新教材修订而被
  静默删除，只会在新版本下失效。
* **版本只追加**。更新术语表会基于旧版本生成新版本，历史版本的摘要可用于
  校验；已经交付的检索快照不依赖“当前版本”。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .text import normalize_text


CANONICAL = "canonical"
LEGACY = "legacy"
ABBREVIATION = "abbreviation"
TERM_TYPES = (CANONICAL, LEGACY, ABBREVIATION)

_TERM_TYPE_ALIASES = {
    "canonical": CANONICAL,
    "standard": CANONICAL,
    "official": CANONICAL,
    "preferred": CANONICAL,
    "规范": CANONICAL,
    "规范名称": CANONICAL,
    "正名": CANONICAL,
    "legacy": LEGACY,
    "old": LEGACY,
    "old_name": LEGACY,
    "deprecated": LEGACY,
    "historical": LEGACY,
    "旧称": LEGACY,
    "曾用名": LEGACY,
    "abbreviation": ABBREVIATION,
    "short": ABBREVIATION,
    "short_name": ABBREVIATION,
    "acronym": ABBREVIATION,
    "简称": ABBREVIATION,
    "缩写": ABBREVIATION,
}


def term_key(value):
    """词面比较键：NFKC 规范化后折叠大小写。"""
    return normalize_text(str(value)).casefold()


def _required(value, field):
    text = "" if value is None else str(value).strip()
    if not text:
        raise ValueError(f"{field}不能为空")
    return text


def _optional(value):
    return "" if value is None else str(value).strip()


def _first(value, names, default=None):
    for name in names:
        if name in value and value[name] is not None:
            return value[name]
    return default


def _timestamp(value, field):
    text = _required(value, field)
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field}不是有效时间：{text}") from error
    return text


def _now(clock):
    value = clock() if callable(clock) else clock
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return _timestamp(str(value), "时间戳")


def _term_type(value):
    raw = "" if value is None else str(value).strip()
    if not raw:
        return CANONICAL
    result = _TERM_TYPE_ALIASES.get(raw) or _TERM_TYPE_ALIASES.get(raw.casefold())
    if result is None:
        raise ValueError(f"未知词条类型：{raw}")
    return result


def _textbook_versions(value):
    if value is None or value == "":
        return ()
    values = [value] if isinstance(value, str) else list(value)
    versions = []
    for item in values:
        version = _optional(item)
        if version and version not in versions:
            versions.append(version)
    if "*" in versions or "any" in [version.casefold() for version in versions]:
        return ()
    return tuple(versions)


@dataclass(frozen=True)
class Concept:
    """一个学科中的概念。术语的学科归属只看这里。"""

    concept_id: str
    subject: str
    canonical_name: str
    definition: str = ""

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise TypeError("概念必须是对象")
        return cls(
            concept_id=_required(_first(value, ("concept_id", "id")), "概念编号"),
            subject=_required(value.get("subject"), "概念学科"),
            canonical_name=_required(
                _first(value, ("canonical_name", "name", "preferred_name")), "概念规范名称"
            ),
            definition=_optional(_first(value, ("definition", "description"), "")),
        )

    def to_dict(self):
        return {
            "concept_id": self.concept_id,
            "subject": self.subject,
            "canonical_name": self.canonical_name,
            "definition": self.definition,
        }


@dataclass(frozen=True)
class TermEntry:
    """词条：某个概念在某个教材版本下的一个词面。"""

    term_id: str
    concept_id: str
    surface: str
    term_type: str = CANONICAL
    textbook_versions: tuple[str, ...] = ()
    note: str = ""

    def valid_for(self, textbook_version):
        if not self.textbook_versions or not textbook_version:
            return True
        wanted = term_key(textbook_version)
        return any(term_key(version) == wanted for version in self.textbook_versions)

    def to_dict(self):
        return {
            "term_id": self.term_id,
            "concept_id": self.concept_id,
            "surface": self.surface,
            "term_type": self.term_type,
            "textbook_versions": list(self.textbook_versions),
            "note": self.note,
        }


def _derived_term_id(concept_id, surface, term_type):
    return f"{concept_id}::{term_type}::{term_key(surface)}"


def _parse_term(value, concepts):
    if not isinstance(value, dict):
        raise TypeError("词条必须是对象")
    concept_id = _required(_first(value, ("concept_id", "concept")), "词条概念编号")
    if concept_id not in concepts:
        raise ValueError(f"词条引用了不存在的概念：{concept_id}")
    surface = _required(_first(value, ("surface", "term", "name", "alias")), "词条词面")
    entry_type = _term_type(_first(value, ("term_type", "kind", "type")))
    versions = _textbook_versions(
        _first(value, ("textbook_versions", "textbook_version", "editions", "valid_versions"))
    )
    declared_subject = _optional(value.get("subject"))
    if declared_subject and term_key(declared_subject) != term_key(concepts[concept_id].subject):
        raise ValueError(
            f"词条“{surface}”声明的学科“{declared_subject}”与概念“{concept_id}”的学科"
            f"“{concepts[concept_id].subject}”不一致；学科必须由概念决定"
        )
    term_id = _optional(_first(value, ("term_id", "entry_id", "id"))) or _derived_term_id(
        concept_id, surface, entry_type
    )
    return TermEntry(
        term_id=term_id,
        concept_id=concept_id,
        surface=surface,
        term_type=entry_type,
        textbook_versions=versions,
        note=_optional(value.get("note")),
    )


def _content_digest(concepts, terms):
    payload = {
        "concepts": [concept.to_dict() for concept in concepts],
        "terms": [term.to_dict() for term in terms],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _parse_content(value):
    if isinstance(value, GlossaryVersion):
        return tuple(value.concepts), tuple(value.terms)
    if not isinstance(value, dict):
        raise TypeError("术语表内容必须是对象")
    raw_concepts = value.get("concepts", []) or []
    raw_terms = value.get("terms", value.get("entries", [])) or []
    if not isinstance(raw_concepts, list) or not isinstance(raw_terms, list):
        raise TypeError("术语表的 concepts 和 terms 必须是列表")

    concepts = []
    concept_map = {}
    for item in raw_concepts:
        concept = Concept.from_dict(item)
        if concept.concept_id in concept_map:
            raise ValueError(f"概念编号重复：{concept.concept_id}")
        concept_map[concept.concept_id] = concept
        concepts.append(concept)

    terms = []
    term_map = {}
    explicit_canonical = set()
    for item in raw_terms:
        term = _parse_term(item, concept_map)
        existing = term_map.get(term.term_id)
        if existing is not None:
            if existing != term:
                raise ValueError(f"词条编号重复：{term.term_id}")
            continue
        term_map[term.term_id] = term
        terms.append(term)
        if term.term_type == CANONICAL:
            explicit_canonical.add((term.concept_id, term_key(term.surface)))

    # 概念的规范名称天然可被识别；即使术语表只登记了旧称/简称也不丢正名。
    for concept in concepts:
        marker = (concept.concept_id, term_key(concept.canonical_name))
        if marker in explicit_canonical:
            continue
        term = TermEntry(
            term_id=_derived_term_id(concept.concept_id, concept.canonical_name, CANONICAL),
            concept_id=concept.concept_id,
            surface=concept.canonical_name,
            term_type=CANONICAL,
        )
        existing = term_map.get(term.term_id)
        if existing is not None:
            if existing != term:
                raise ValueError(f"词条编号重复：{term.term_id}")
            continue
        term_map[term.term_id] = term
        terms.append(term)

    concepts.sort(key=lambda item: item.concept_id)
    terms.sort(key=lambda item: (item.concept_id, term_key(item.surface), item.term_type, item.term_id))
    return tuple(concepts), tuple(terms)


@dataclass(frozen=True)
class GlossaryVersion:
    """一个不可变的术语表版本。"""

    version_id: str
    revision: int
    effective_at: str
    created_at: str
    concepts: tuple[Concept, ...]
    terms: tuple[TermEntry, ...]
    digest: str

    @classmethod
    def build(cls, *, version_id, revision, effective_at, created_at, concepts, terms):
        concepts = tuple(concepts)
        terms = tuple(terms)
        return cls(
            version_id=_required(version_id, "术语表版本编号"),
            revision=int(revision),
            effective_at=_timestamp(effective_at, "术语表生效时间"),
            created_at=_timestamp(created_at, "术语表创建时间"),
            concepts=concepts,
            terms=terms,
            digest=_content_digest(concepts, terms),
        )

    @classmethod
    def from_dict(cls, value):
        if not isinstance(value, dict):
            raise TypeError("术语表版本必须是对象")
        concepts, terms = _parse_content(value)
        version = cls(
            version_id=_required(_first(value, ("version_id", "id")), "术语表版本编号"),
            revision=int(value.get("revision", 1)),
            effective_at=_timestamp(
                _first(value, ("effective_at", "created_at")), "术语表生效时间"
            ),
            created_at=_timestamp(
                _first(value, ("created_at", "effective_at")), "术语表创建时间"
            ),
            concepts=concepts,
            terms=terms,
            digest=_optional(value.get("digest")) or _content_digest(concepts, terms),
        )
        expected = _content_digest(version.concepts, version.terms)
        if version.digest != expected:
            raise ValueError(f"术语表版本摘要不匹配：{version.version_id}")
        if version.revision < 1:
            raise ValueError(f"术语表版本修订号无效：{version.version_id}")
        return version

    def to_dict(self):
        return {
            "version_id": self.version_id,
            "revision": self.revision,
            "effective_at": self.effective_at,
            "created_at": self.created_at,
            "digest": self.digest,
            "concepts": [concept.to_dict() for concept in self.concepts],
            "terms": [term.to_dict() for term in self.terms],
        }

    def concept_map(self):
        return {concept.concept_id: concept for concept in self.concepts}

    def summary(self):
        return {
            "version_id": self.version_id,
            "revision": self.revision,
            "effective_at": self.effective_at,
            "created_at": self.created_at,
            "digest": self.digest,
            "concept_count": len(self.concepts),
            "term_count": len(self.terms),
        }

    def snapshot(self):
        """供检索快照内嵌的完整版本副本，保证历史查询可离线解释。"""
        value = self.to_dict()
        value["summary"] = self.summary()
        return value


class GlossaryRepository:
    """只追加的术语表版本库。"""

    def __init__(self, path, clock=None):
        self.path = Path(path)
        self.clock = clock

    def _read(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        raw = value if isinstance(value, list) else value.get("versions") if isinstance(value, dict) else None
        if not isinstance(raw, list):
            raise ValueError("术语表版本库格式无效")
        versions = [GlossaryVersion.from_dict(item) for item in raw]
        version_ids = [version.version_id for version in versions]
        if len(set(version_ids)) != len(version_ids):
            raise ValueError("术语表版本库包含重复版本编号")
        revisions = [version.revision for version in versions]
        if len(set(revisions)) != len(revisions):
            raise ValueError("术语表版本库包含重复修订号")
        return versions

    def _write(self, versions):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = {"versions": [version.to_dict() for version in versions]}
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def versions(self):
        return sorted(self._read(), key=lambda version: version.revision)

    def active(self):
        """最新发布的版本（用于更新基线，不代表已经生效）。"""
        versions = self.versions()
        return versions[-1] if versions else None

    def active_version(self):
        version = self.active()
        if version is None:
            raise LookupError("术语表尚未建立任何版本")
        return version

    def effective(self, at=None):
        """当前实际生效的版本；未来生效的版本不会提前影响检索。"""
        moment = _now(self.clock) if at is None else _timestamp(at, "查询时间")
        return self.at(moment)

    def get(self, selector):
        if selector is None:
            return None
        text = str(selector).strip()
        for version in self.versions():
            if version.version_id == text:
                return version
        try:
            revision = int(text)
        except ValueError:
            return None
        for version in self.versions():
            if version.revision == revision:
                return version
        return None

    def at(self, as_of):
        """返回在给定时间点既已生效、又已发布的版本。

        ``effective_at`` 可以回溯填写，但只有 ``created_at`` 也在 ``as_of`` 之前
        的版本才会被选中，因此补录历史生效时间不会改写已经过去的查询解释。
        """
        if as_of is None:
            return self.active_version()
        moment = _as_datetime(_timestamp(as_of, "查询时间"))
        eligible = [
            version
            for version in self.versions()
            if _as_datetime(version.effective_at) <= moment
            and _as_datetime(version.created_at) <= moment
        ]
        if not eligible:
            raise LookupError(f"在 {as_of} 之前没有已生效并已发布的术语表版本")
        return max(
            eligible,
            key=lambda version: (
                _as_datetime(version.effective_at),
                _as_datetime(version.created_at),
                version.revision,
            ),
        )

    def publish(self, payload, *, version_id=None, effective_at=None):
        versions = self.versions()
        revision = max((version.revision for version in versions), default=0) + 1
        if isinstance(payload, GlossaryVersion):
            payload = payload.to_dict()
        if not isinstance(payload, dict):
            raise TypeError("术语表内容必须是对象")
        chosen_id = _optional(version_id) or _optional(_first(payload, ("version_id", "id")))
        chosen_id = chosen_id or f"glossary-{revision:04d}"
        if any(version.version_id == chosen_id for version in versions):
            raise ValueError(f"术语表版本编号已存在：{chosen_id}")
        effective = _optional(effective_at) or _optional(payload.get("effective_at"))
        created_at = _now(self.clock)
        effective = effective or created_at
        concepts, terms = _parse_content(payload)
        version = GlossaryVersion.build(
            version_id=chosen_id,
            revision=revision,
            effective_at=effective,
            created_at=created_at,
            concepts=concepts,
            terms=terms,
        )
        self._write(versions + [version])
        return version

    def update(
        self,
        *,
        add_concepts=(),
        add_terms=(),
        remove_term_ids=(),
        base_version=None,
        version_id=None,
        effective_at=None,
    ):
        """基于已有版本追加新版本；旧版本保持不变。"""
        if base_version is not None:
            base = self.get(base_version)
            if base is None:
                raise ValueError(f"找不到基础术语表版本：{base_version}")
        else:
            base = self.active()
            if base is None:
                raise ValueError("没有可更新的术语表版本")
        concepts = {concept.concept_id: concept.to_dict() for concept in base.concepts}
        for item in add_concepts:
            concept = Concept.from_dict(item)
            concepts[concept.concept_id] = concept.to_dict()
        concept_map = {key: Concept.from_dict(value) for key, value in concepts.items()}
        terms = {term.term_id: term.to_dict() for term in base.terms}
        for term_id in remove_term_ids:
            term_id = str(term_id)
            if term_id not in terms:
                raise ValueError(f"待删除词条不存在：{term_id}")
            if terms[term_id]["term_type"] == CANONICAL:
                raise ValueError(
                    f"不能删除概念的规范名称词条：{term_id}；如要改名请修改概念并添加新的规范词条"
                )
            del terms[term_id]
        for item in add_terms:
            term = _parse_term(item, concept_map)
            terms[term.term_id] = term.to_dict()
        return self.publish(
            {"concepts": list(concepts.values()), "terms": list(terms.values())},
            version_id=version_id,
            effective_at=effective_at,
        )


def _as_datetime(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


@dataclass(frozen=True)
class Resolution:
    """单个词面的解析结果；歧义与无定义都保留候选和原因。"""

    surface: str
    status: str
    subject: str | None
    textbook_version: str | None
    concept_id: str | None
    concepts: tuple[dict, ...]
    terms: tuple[dict, ...]
    uncertainties: tuple[dict, ...]
    reason: str | None = None

    def to_dict(self):
        return {
            "surface": self.surface,
            "status": self.status,
            "reason": self.reason,
            "subject": self.subject,
            "textbook_version": self.textbook_version,
            "concept_id": self.concept_id,
            "concept": self.concepts[0] if self.concept_id else None,
            "concepts": list(self.concepts),
            "terms": list(self.terms),
            "uncertainties": list(self.uncertainties),
        }


class GlossaryIndex:
    """在某个术语表版本上做“词面 → 概念”的确定性解析与文本识别。"""

    def __init__(self, version):
        self.version = version
        self._concepts = version.concept_map()
        self._by_surface = {}
        for term in version.terms:
            self._by_surface.setdefault(term_key(term.surface), []).append(term)
        surfaces = sorted(self._by_surface, key=lambda item: (-len(item), item))
        self._pattern = (
            re.compile("|".join(re.escape(surface) for surface in surfaces), re.IGNORECASE)
            if surfaces
            else None
        )

    def surfaces(self):
        return list(self._by_surface)

    def concept(self, concept_id):
        return self._concepts.get(concept_id)

    def concept_dict(self, concept_id):
        concept = self._concepts[concept_id]
        return concept.to_dict()

    def _term_dict(self, term):
        concept = self._concepts[term.concept_id]
        value = term.to_dict()
        value["subject"] = concept.subject
        value["concept_definition"] = concept.definition
        return value

    def _resolution(
        self,
        surface,
        status,
        *,
        subject,
        textbook_version,
        concept_id=None,
        entries=(),
        uncertainties=(),
        reason=None,
    ):
        concepts = []
        for entry in entries:
            if entry.concept_id in [item["concept_id"] for item in concepts]:
                continue
            value = self.concept_dict(entry.concept_id)
            value["term_ids"] = [
                candidate.term_id
                for candidate in entries
                if candidate.concept_id == entry.concept_id
            ]
            concepts.append(value)
        if concept_id and concept_id not in [item["concept_id"] for item in concepts]:
            concepts.insert(0, self.concept_dict(concept_id))
        return Resolution(
            surface=surface,
            status=status,
            subject=subject,
            textbook_version=textbook_version,
            concept_id=concept_id,
            concepts=tuple(concepts),
            terms=tuple(self._term_dict(entry) for entry in entries),
            uncertainties=tuple(uncertainties),
            reason=reason,
        )

    def resolve(self, surface, *, subject=None, textbook_version=None):
        """解析一个词面。

        ``subject`` 为空时，同一词面跨学科出现会返回 ``ambiguous``，不会挑一个
        默认学科。``textbook_version`` 为空时，只在同一学科内按版本分化的词条
        会返回歧义并附带 ``textbook_version_unspecified``。
        """
        surface = str(surface)
        all_terms = list(self._by_surface.get(term_key(surface), []))
        subject = _optional(subject) or None
        textbook_version = _optional(textbook_version) or None
        if not all_terms:
            return self._resolution(
                surface,
                "undefined",
                subject=subject,
                textbook_version=textbook_version,
                uncertainties=(
                    {
                        "code": "no_valid_definition",
                        "message": f"术语表中没有词面“{surface}”的定义",
                        "reason": "unknown_surface",
                    },
                ),
                reason="unknown_surface",
            )

        candidates = all_terms
        if subject is not None:
            candidates = [
                term
                for term in all_terms
                if term_key(self._concepts[term.concept_id].subject) == term_key(subject)
            ]
            if not candidates:
                return self._resolution(
                    surface,
                    "undefined",
                    subject=subject,
                    textbook_version=textbook_version,
                    entries=all_terms,
                    uncertainties=(
                        {
                            "code": "no_valid_definition",
                            "message": f"词面“{surface}”在学科“{subject}”中没有定义",
                            "reason": "subject_mismatch",
                            "other_subjects": sorted(
                                {self._concepts[term.concept_id].subject for term in all_terms}
                            ),
                        },
                    ),
                    reason="subject_mismatch",
                )

        valid_terms = [term for term in candidates if term.valid_for(textbook_version)]
        if not valid_terms:
            return self._resolution(
                surface,
                "undefined",
                subject=subject,
                textbook_version=textbook_version,
                entries=candidates,
                uncertainties=(
                    {
                        "code": "no_valid_definition",
                        "message": (
                            f"词面“{surface}”在教材版本“{textbook_version}”下没有有效定义"
                            if textbook_version
                            else f"词面“{surface}”没有适用于当前教材版本的定义"
                        ),
                        "reason": "textbook_version_mismatch",
                    },
                ),
                reason="textbook_version_mismatch",
            )

        concept_ids = []
        for term in valid_terms:
            if term.concept_id not in concept_ids:
                concept_ids.append(term.concept_id)
        subjects = {self._concepts[term.concept_id].subject for term in valid_terms}

        if len(concept_ids) > 1:
            if subject is None and len(subjects) > 1:
                code = "ambiguous_cross_subject"
                message = (
                    f"词面“{surface}”在多个学科中分别表示不同概念，必须指定学科"
                )
            elif textbook_version is None:
                code = "ambiguous_textbook_version"
                message = (
                    f"词面“{surface}”在不同教材版本下对应不同概念，必须指定教材版本"
                )
            else:
                code = "ambiguous_concept"
                message = f"词面“{surface}”对应多个概念，不能按词面直接归并"
            uncertainties = [
                {
                    "code": code,
                    "message": message,
                    "subjects": sorted(subjects),
                    "concepts": concept_ids,
                }
            ]
            if subject is None and len(subjects) > 1:
                uncertainties.append(
                    {
                        "code": "subject_required",
                        "message": f"请在查询中指定学科以解释“{surface}”",
                    }
                )
            if textbook_version is None and code != "ambiguous_cross_subject":
                uncertainties.append(
                    {
                        "code": "textbook_version_unspecified",
                        "message": f"“{surface}”的概念归属依赖教材版本",
                    }
                )
            return self._resolution(
                surface,
                "ambiguous",
                subject=subject,
                textbook_version=textbook_version,
                entries=valid_terms,
                uncertainties=uncertainties,
            )

        concept_id = concept_ids[0]
        applicable = [term for term in valid_terms if term.concept_id == concept_id]
        uncertainties = []
        legacy = [term for term in applicable if term.term_type == LEGACY]
        if legacy:
            uncertainties.append(
                {
                    "code": "legacy_term",
                    "message": f"“{surface}”是旧称，仅适用于历史教材版本",
                    "concept_id": concept_id,
                    "textbook_versions": sorted(
                        {version for term in legacy for version in term.textbook_versions}
                    ),
                }
            )
            if textbook_version is None:
                uncertainties.append(
                    {
                        "code": "textbook_version_unspecified",
                        "message": f"旧称“{surface}”未限定教材版本，需核对使用年代",
                        "concept_id": concept_id,
                    }
                )
        abbreviation = [term for term in applicable if term.term_type == ABBREVIATION]
        if abbreviation:
            uncertainties.append(
                {
                    "code": "abbreviation",
                    "message": f"“{surface}”是简称，已按术语表归入概念“{concept_id}”",
                    "concept_id": concept_id,
                }
            )
        return self._resolution(
            surface,
            "resolved",
            subject=subject,
            textbook_version=textbook_version,
            concept_id=concept_id,
            entries=applicable,
            uncertainties=uncertainties,
        )

    def recognize(self, text, *, subject=None, textbook_version=None):
        """在一段文本中识别术语提及；每个提及都带概念解析与原文位置。"""
        text = normalize_text(str(text))
        if self._pattern is None or not text:
            return []
        mentions = []
        for match in self._pattern.finditer(text):
            surface = match.group(0)
            resolution = self.resolve(
                surface, subject=subject, textbook_version=textbook_version
            ).to_dict()
            resolution["matched_text"] = surface
            resolution["span"] = {"start": match.start(), "end": match.end()}
            mentions.append(resolution)
        return mentions
