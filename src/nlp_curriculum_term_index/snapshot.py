"""检索快照归档：只追加，已交付快照不会被术语表更新改写。"""

from __future__ import annotations

import json
from pathlib import Path


class SnapshotArchiveError(ValueError):
    pass


class SnapshotArchive:
    def __init__(self, path):
        self.path = Path(path)

    def _read(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        if not isinstance(value, dict) or not isinstance(value.get("snapshots"), list):
            raise SnapshotArchiveError("检索快照库格式无效")
        return value["snapshots"]

    def _write(self, snapshots):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"snapshots": snapshots}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def save(self, snapshot):
        snapshot_id = snapshot.get("snapshot_id")
        if not snapshot_id:
            raise SnapshotArchiveError("快照缺少编号")
        # 归档前核对编号与内容一致，避免把被改写过的结果当作已交付快照。
        from .query import snapshot_id as compute_snapshot_id

        if compute_snapshot_id(snapshot) != snapshot_id:
            raise SnapshotArchiveError(f"快照内容与编号不一致：{snapshot_id}")
        snapshots = self._read()
        for existing in snapshots:
            if existing.get("snapshot_id") == snapshot_id:
                if existing != snapshot:
                    raise SnapshotArchiveError(f"快照编号冲突：{snapshot_id}")
                return existing
        self._write(snapshots + [snapshot])
        return snapshot

    def save_many(self, snapshots):
        return [self.save(snapshot) for snapshot in snapshots]

    def get(self, snapshot_id):
        for snapshot in self._read():
            if snapshot.get("snapshot_id") == snapshot_id:
                return snapshot
        return None

    def find(self, *, record_id=None, context=None, subject=None, glossary_version_id=None):
        matches = []
        for snapshot in self._read():
            query = snapshot.get("query", {})
            if subject and query.get("subject") != subject:
                continue
            if context and query.get("record_context") != context:
                continue
            if glossary_version_id and snapshot.get("glossary", {}).get("version_id") != glossary_version_id:
                continue
            if record_id and not any(
                result.get("record_id") == record_id for result in snapshot.get("results", [])
            ):
                continue
            matches.append(snapshot)
        return matches

    def all(self):
        return self._read()
