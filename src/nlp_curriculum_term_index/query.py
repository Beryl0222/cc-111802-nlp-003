"""检索：按概念而不是按字面命中，并把结果冻结为可解释的快照。

一次查询的流程：

1. 选定术语表版本（默认当前有效版本；``as_of`` 用当时生效的版本）。
2. 把查询词解析成概念；跨学科歧义保留为不确定性，不生成全局同义词。
3. 对每条课案，用**该课案自己的学科和教材版本**解析词面，只有落到同一概念
   才算命中；旧称/简称命中也保留其原文与来源版本。
4. 把使用的术语表版本、概念明细、命中原文和不确定性一并写入快照。快照之后
   不再修改，术语表更新只影响新的查询。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .glossary import GlossaryIndex, GlossaryVersion, term_key
from .text import normalize_text


def _clock_value(clock):
    value = clock() if callable(clock) else clock
    if value is None:
        value = datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"查询时间无效：{value}") from error
    return str(value)


def _split_query(query):
    """把查询按标点和空白拆成词面。连接词留给 ``_expand_terms`` 处理。"""
    text = normalize_text(str(query))
    terms = []
    current = []
    for char in text:
        if char.isspace() or char in "，。；;,.!?！？、（）()[]【】":
            if current:
                terms.append("".join(current).strip())
                current = []
        else:
            current.append(char)
    if current:
        terms.append("".join(current).strip())
    return [term for term in terms if term]


def _expand_terms(index, terms, subject, textbook_version):
    """把“分数和比例”这类查询拆开，但只在整词没有定义时才拆。

    这样既支持用连接词组合多个术语，也不会把本身含“和/与”的词面误拆。
    """
    expanded = []
    for term in terms:
        resolution = index.resolve(
            term, subject=subject, textbook_version=textbook_version
        )
        if resolution.status != "undefined":
            if term not in expanded:
                expanded.append(term)
            continue
        pieces = []
        buffer = []
        for char in term:
            if char in "和与或及":
                if buffer:
                    pieces.append("".join(buffer))
                    buffer = []
            else:
                buffer.append(char)
        if buffer:
            pieces.append("".join(buffer))
        usable = [
            piece
            for piece in pieces
            if piece
            and index.resolve(
                piece, subject=subject, textbook_version=textbook_version
            ).status
            != "undefined"
        ]
        if len(usable) == len(pieces) and len(usable) > 1:
            for piece in usable:
                if piece not in expanded:
                    expanded.append(piece)
        elif term not in expanded:
            expanded.append(term)
    return expanded


def _quote(text, start, end):
    return {
        "before": text[max(0, start - 24) : start],
        "text": text[start:end],
        "after": text[end : end + 24],
    }


def _record_scope(record, query_subject, query_version):
    subject = str(record.get("subject", "") or "").strip() or query_subject
    version = (
        str(record.get("textbook_version", "") or "").strip()
        or str(record.get("textbook_edition", "") or "").strip()
        or query_version
    )
    subject_source = "record" if str(record.get("subject", "") or "").strip() else (
        "query" if query_subject else None
    )
    version_source = "record" if (
        str(record.get("textbook_version", "") or "").strip()
        or str(record.get("textbook_edition", "") or "").strip()
    ) else ("query" if query_version else None)
    return subject or None, version or None, subject_source, version_source


def _unique(values):
    result = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


class QueryService:
    def __init__(self, repository, glossary, clock=None):
        self.repository = repository
        self.glossary = glossary
        self.clock = clock

    # -- 版本选择 ---------------------------------------------------------

    def _version(self, as_of=None):
        # 默认使用“此刻有效”的版本；未来才生效的版本不会提前改变检索结果。
        moment = as_of if as_of is not None else _clock_value(self.clock)
        return self.glossary.at(moment)

    # -- 查询词解析 -------------------------------------------------------

    def _resolve_query_terms(self, index, terms, subject, textbook_version):
        resolved = []
        for term in terms:
            resolution = index.resolve(
                term, subject=subject, textbook_version=textbook_version
            ).to_dict()
            resolved.append(resolution)
        return resolved

    @staticmethod
    def _query_targets(term_resolutions):
        """查询层面固定下来的目标概念。"""
        targets = {}
        for resolution in term_resolutions:
            term = resolution["surface"]
            if resolution["status"] == "resolved":
                targets[term] = [resolution["concept_id"]]
            elif resolution["status"] == "ambiguous":
                targets[term] = [
                    concept["concept_id"] for concept in resolution["concepts"]
                ]
            else:
                targets[term] = []
        return targets

    # -- 单条记录的匹配 ---------------------------------------------------

    def _match_record(self, index, record, query_terms, query_targets, query_subject, query_version):
        record_text = normalize_text(record.get("text", ""))
        subject, version, subject_source, version_source = _record_scope(
            record, query_subject, query_version
        )

        # 查询词指向的概念集合在查询层面确定；记录只负责在**自己的学科和教材
        # 版本**下解释词面，不能反过来改变查询词的含义。查询词自身的歧义只在
        # 快照层面报告一次，不复制到每条记录。
        target_concepts = set()
        for term in query_terms:
            target_concepts.update(query_targets.get(term, []))

        # 需要扫描的词面：查询词本身，以及目标概念在该记录教材版本下的所有
        # 词条（这样旧称、简称也能按概念命中）。
        surfaces = set(query_terms)
        for concept_id in target_concepts:
            for term in index.version.terms:
                if term.concept_id == concept_id and term.valid_for(version):
                    surfaces.add(term.surface)
        surface_pattern = _surface_pattern(surfaces)

        matches = []
        uncertain = []
        mismatched = []
        for match in surface_pattern.finditer(record_text):
            surface = match.group(0)
            mention = index.resolve(
                surface, subject=subject, textbook_version=version
            ).to_dict()
            entry = {
                "surface": surface,
                "matched_text": surface,
                "span": {"start": match.start(), "end": match.end()},
                "quote": _quote(record_text, match.start(), match.end()),
                "status": mention["status"],
                "reason": mention["reason"],
                "concept_id": mention["concept_id"],
                "concept": mention["concept"],
                "candidates": mention["concepts"],
                "terms": mention["terms"],
                "uncertainties": mention["uncertainties"],
                "record": {
                    "record_id": record["record_id"],
                    "source": record["source"],
                    "revision": record["revision"],
                    "observed_at": record["observed_at"],
                    "subject": subject,
                    "subject_source": subject_source,
                    "textbook_version": version,
                    "textbook_version_source": version_source,
                    "context": record.get("context", ""),
                },
            }
            if mention["status"] != "resolved":
                entry["uncertainty_code"] = (
                    "ambiguous_concept"
                    if mention["status"] == "ambiguous"
                    else "no_valid_definition"
                )
                entry["query_terms"] = [surface]
                uncertain.append(entry)
            elif mention["concept_id"] in target_concepts:
                entry["query_terms"] = [
                    term
                    for term in query_terms
                    if mention["concept_id"] in query_targets.get(term, [])
                ] or [surface]
                matches.append(entry)
            else:
                # 词面命中了，但在该记录自己的学科/教材版本下是另一个概念：
                # 明确不命中，不并入结果。
                entry["uncertainty_code"] = "concept_mismatch"
                entry["query_terms"] = [surface]
                mismatched.append(entry)

        # 只命中其他概念、或查询词根本没有出现的记录不进入结果。
        if not matches and not uncertain:
            return None

        matched_terms = _unique(
            [entry["matched_text"] for entry in matches]
        )
        concepts = []
        for concept_id in _unique([entry["concept_id"] for entry in matches]):
            concept = index.concept_dict(concept_id)
            concept["matched_terms"] = _unique(
                entry["matched_text"] for entry in matches if entry["concept_id"] == concept_id
            )
            value = {
                "concept_id": concept_id,
                "subject": concept["subject"],
                "canonical_name": concept["canonical_name"],
                "definition": concept["definition"],
                "matched_terms": concept["matched_terms"],
            }
            concepts.append(value)

        status = "matched" if matches and not uncertain else (
            "uncertain" if uncertain and not matches else "partial"
        )
        result = {
            "record_id": record["record_id"],
            "source": record["source"],
            "revision": record["revision"],
            "observed_at": record["observed_at"],
            "context": record.get("context", ""),
            "subject": subject,
            "subject_source": subject_source,
            "textbook_version": version,
            "textbook_version_source": version_source,
            "match_status": status,
            "matched_terms": matched_terms,
            "query_terms": [
                term for term in query_terms if query_targets.get(term)
            ],
            "concepts": concepts,
            "matches": matches,
            "uncertain_mentions": uncertain,
            "mismatched_mentions": mismatched,
            "uncertainties": [],
        }
        if uncertain:
            result["uncertainties"].append(
                {
                    "code": "ambiguous_or_unconfirmed_mentions",
                    "message": (
                        f"记录“{record['record_id']}”中有无法唯一确认归属的词面，"
                        "已保留原文但未计入确定命中"
                    ),
                    "surfaces": _unique([entry["surface"] for entry in uncertain]),
                }
            )
        if subject_source == "query" and matches:
            result["uncertainties"].append(
                {
                    "code": "subject_inferred_from_query",
                    "message": (
                        f"记录“{record['record_id']}”没有学科标注，按查询学科"
                        f"“{subject}”解释词面"
                    ),
                    "subject": subject,
                }
            )
        if (
            version_source == "record"
            and query_version
            and version
            and term_key(version) != term_key(query_version)
        ):
            result["uncertainties"].append(
                {
                    "code": "textbook_version_differs_from_query",
                    "message": (
                        f"记录“{record['record_id']}”标注的教材版本是“{version}”，"
                        f"与查询的“{query_version}”不同；命中按记录版本解释"
                    ),
                    "record_textbook_version": version,
                    "query_textbook_version": query_version,
                }
            )
        return result

    # -- 快照 -------------------------------------------------------------

    def search(
        self,
        query,
        *,
        subject=None,
        textbook_version=None,
        record_context=None,
        source=None,
        as_of=None,
    ):
        query_text = str(query)
        terms = _split_query(query_text)
        if not terms:
            raise ValueError("查询不能为空")
        subject = str(subject).strip() if subject else None
        textbook_version = str(textbook_version).strip() if textbook_version else None
        record_context = str(record_context).strip() if record_context else None

        version = self._version(as_of)
        index = GlossaryIndex(version)
        queried_at = _clock_value(self.clock)
        terms = _expand_terms(index, terms, subject, textbook_version)
        term_resolutions = self._resolve_query_terms(index, terms, subject, textbook_version)
        query_targets = self._query_targets(term_resolutions)

        records = self.repository.read()
        if source:
            records = [record for record in records if record.get("source") == source]
        if record_context:
            records = [
                record
                for record in records
                if term_key(record.get("context", "")) == term_key(record_context)
            ]

        results = []
        for record in records:
            result = self._match_record(
                index, record, terms, query_targets, subject, textbook_version
            )
            if result is not None:
                results.append(result)

        snapshot = {
            "snapshot_id": "",
            "query": {
                "text": query_text,
                "terms": terms,
                "subject": subject,
                "textbook_version": textbook_version,
                "record_context": record_context,
                "source": source,
                "as_of": as_of,
                "queried_at": queried_at,
            },
            "glossary": {
                **version.snapshot(),
                "selection": {
                    "mode": "as_of" if as_of is not None else "effective_now",
                    "as_of": as_of if as_of is not None else queried_at,
                },
            },
            "term_resolutions": term_resolutions,
            "policy": {
                "concept_keyed": True,
                "scope": "record.subject + record.textbook_version",
                "cross_subject": "never_merged",
                "ambiguity": "reported_not_resolved",
                "snapshot": "immutable_until_explicit_requery",
            },
            "results": results,
            "uncertainties": self._snapshot_uncertainties(
                terms, subject, textbook_version, term_resolutions, results
            ),
            "result_count": len(results),
        }
        snapshot["snapshot_id"] = snapshot_id(snapshot)
        return snapshot

    def _snapshot_uncertainties(
        self, terms, subject, textbook_version, term_resolutions, results
    ):
        items = []
        for resolution in term_resolutions:
            for uncertainty in resolution["uncertainties"]:
                if uncertainty["code"] in (
                    "ambiguous_cross_subject",
                    "ambiguous_textbook_version",
                    "ambiguous_concept",
                    "no_valid_definition",
                    "legacy_term",
                    "abbreviation",
                    "textbook_version_unspecified",
                ):
                    items.append(
                        {
                            "scope": "query_term",
                            "term": resolution["surface"],
                            **uncertainty,
                        }
                    )
        for result in results:
            for uncertainty in result["uncertainties"]:
                items.append(
                    {
                        "scope": "record",
                        "record_id": result["record_id"],
                        **uncertainty,
                    }
                )
            for match in result["matches"]:
                for uncertainty in match["uncertainties"]:
                    if uncertainty["code"] in ("legacy_term", "abbreviation"):
                        items.append(
                            {
                                "scope": "record_mention",
                                "record_id": result["record_id"],
                                "surface": match["surface"],
                                "concept_id": match["concept_id"],
                                **uncertainty,
                            }
                        )
        if subject is None:
            if any(
                item.get("code") == "ambiguous_cross_subject"
                for item in items
                if item.get("scope") == "query_term"
            ):
                items.insert(
                    0,
                    {
                        "scope": "query",
                        "code": "subject_required",
                        "message": "查询词涉及多个学科；结果按各课案自己的学科归属，未合并概念",
                    },
                )
        return items


def _surface_pattern(surfaces):
    import re

    ordered = sorted(surfaces, key=lambda value: (-len(value), value))
    return re.compile("|".join(re.escape(value) for value in ordered), re.IGNORECASE)


def canonical_snapshot_payload(snapshot):
    """去掉快照编号后的规范化 JSON，用于计算稳定编号。"""
    value = dict(snapshot)
    value.pop("snapshot_id", None)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def snapshot_id(snapshot):
    digest = hashlib.sha256(canonical_snapshot_payload(snapshot).encode("utf-8")).hexdigest()
    return "snapshot-" + digest[:20]


def replay_snapshot(snapshot):
    """检查快照是否自洽：内嵌术语表版本摘要与内容一致，且编号可复算。"""
    value = dict(snapshot["glossary"])
    value.pop("selection", None)
    version = GlossaryVersion.from_dict(value)
    expected = snapshot_id(snapshot)
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "recomputable": expected == snapshot["snapshot_id"],
        "glossary_version_id": version.version_id,
        "glossary_digest": version.digest,
        "glossary_intact": version.digest == snapshot["glossary"]["summary"]["digest"],
        "queried_at": snapshot["query"]["queried_at"],
        "selection": snapshot["glossary"].get("selection"),
        "results": [
            {
                "record_id": result["record_id"],
                "match_status": result["match_status"],
                "matched_terms": result["matched_terms"],
                "concepts": [concept["concept_id"] for concept in result["concepts"]],
                "evidence": [
                    {
                        "record_id": match["record"]["record_id"],
                        "revision": match["record"]["revision"],
                        "textbook_version": match["record"]["textbook_version"],
                        "start": match["span"]["start"],
                        "end": match["span"]["end"],
                        "text": match["matched_text"],
                    }
                    for match in result["matches"]
                ],
                "uncertainties": result["uncertainties"],
            }
            for result in snapshot["results"]
        ],
    }
