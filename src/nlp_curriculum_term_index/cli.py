import argparse
import json
import sys
from pathlib import Path

from .glossary import GlossaryRepository
from .query import QueryService, replay_snapshot
from .repository import RecordRepository
from .snapshot import SnapshotArchive
from .workflow import TextWorkflow


def _load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地教研文本处理工具")
    parser.add_argument("action", choices=(
        "ingest",
        "list",
        "glossary-publish",
        "glossary-update",
        "glossary-show",
        "search",
        "snapshot-list",
        "snapshot-show",
        "snapshot-replay",
    ))
    parser.add_argument("value", nargs="?", help="记录文件、术语表文件、查询词或快照编号")
    parser.add_argument("--state", default="var/records.json", help="课案记录库")
    parser.add_argument("--glossary", default="var/glossary.json", help="术语表版本库")
    parser.add_argument("--snapshots", default="var/snapshots.json", help="检索快照归档")
    parser.add_argument("--subject", help="学科（不指定时保留跨学科歧义）")
    parser.add_argument("--textbook-version", help="教材版本")
    parser.add_argument("--context", help="课案语境")
    parser.add_argument("--source", help="来源")
    parser.add_argument("--as-of", help="使用该时间点生效的术语表版本")
    parser.add_argument("--base-version", help="术语表更新所基于的版本编号或修订号")
    parser.add_argument("--version-id", help="新术语表版本编号")
    parser.add_argument("--effective-at", help="新术语表版本生效时间")
    parser.add_argument("--save", action="store_true", help="把检索结果写入快照归档")
    args = parser.parse_args(argv)

    records = RecordRepository(args.state)

    if args.action == "ingest":
        glossary = GlossaryRepository(args.glossary) if Path(args.glossary).exists() else None
        result = TextWorkflow(records, glossary=glossary).ingest(_load(args.value or "fixtures/records.json"))
    elif args.action == "list":
        result = TextWorkflow(records).list_records(
            source=args.source,
            subject=args.subject,
            textbook_version=args.textbook_version,
        )
    elif args.action == "glossary-publish":
        if not args.value:
            parser.error("glossary-publish 需要术语表文件")
        result = GlossaryRepository(args.glossary).publish(
            _load(args.value),
            version_id=args.version_id,
            effective_at=args.effective_at,
        ).summary()
    elif args.action == "glossary-update":
        if not args.value:
            parser.error("glossary-update 需要补丁文件")
        patch = _load(args.value)
        result = GlossaryRepository(args.glossary).update(
            add_concepts=patch.get("add_concepts", []),
            add_terms=patch.get("add_terms", []),
            remove_term_ids=patch.get("remove_term_ids", []),
            base_version=args.base_version,
            version_id=args.version_id,
            effective_at=args.effective_at,
        ).summary()
    elif args.action == "glossary-show":
        repository = GlossaryRepository(args.glossary)
        version = repository.get(args.value) if args.value else repository.active()
        if version is None:
            result = {"versions": [item.summary() for item in repository.versions()]}
        else:
            result = version.to_dict()
    elif args.action == "search":
        if not args.value:
            parser.error("search 需要查询词")
        service = QueryService(records, GlossaryRepository(args.glossary))
        snapshot = service.search(
            args.value,
            subject=args.subject,
            textbook_version=args.textbook_version,
            record_context=args.context,
            source=args.source,
            as_of=args.as_of,
        )
        if args.save:
            SnapshotArchive(args.snapshots).save(snapshot)
        result = snapshot
    elif args.action == "snapshot-list":
        result = [
            {
                "snapshot_id": item["snapshot_id"],
                "query": item["query"],
                "glossary_version_id": item["glossary"]["version_id"],
                "result_count": item.get("result_count", len(item.get("results", []))),
            }
            for item in SnapshotArchive(args.snapshots).all()
        ]
    elif args.action == "snapshot-show":
        result = SnapshotArchive(args.snapshots).get(args.value)
        if result is None:
            parser.error(f"找不到快照：{args.value}")
    else:  # snapshot-replay
        snapshot = SnapshotArchive(args.snapshots).get(args.value)
        if snapshot is None:
            parser.error(f"找不到快照：{args.value}")
        result = replay_snapshot(snapshot)
    _print(result)


if __name__ == "__main__":
    sys.exit(main())
