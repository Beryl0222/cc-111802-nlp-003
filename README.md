# 课程术语索引

课程术语索引用于处理本地合成教研文本，保存来源版本、文本片段和处理记录，供校内复核使用。

## 环境

需要 Python 3.11 或更新版本。应用运行不依赖第三方包；运行测试和构建前，安装固定版本的开发工具：`python -m pip install -r requirements-dev.txt`。

## 概念归属

术语表的**概念**是唯一的学科归属：概念决定学科，词条（规范名称、旧称、简称）只
指向概念，不能自带一套与之冲突的学科。这样术语识别、教材版本过滤和检索结果使用
同一套概念归属。

* 同一词面可以在不同学科指向不同概念。查询不指定学科时，各课案按**自己的学科**
  解释词面，结果不会把不同学科的概念合并，跨学科歧义写入 `uncertainties`。
* 旧称可限定 `textbook_versions`。新教材下旧称只对历史版本有效；旧称命中会保留
  命中原文、`term_type`、来源修订号和教材版本，并标记 `legacy_term`。
* 没有有效定义（未收录词面、学科不符、教材版本失效）不会静默按字面命中，而是
  返回 `undefined` / `no_valid_definition` 及候选概念。

术语表版本只追加：`glossary-update` 基于旧版本生成新版本，旧版本内容与摘要保持
不变。版本带 `effective_at`，检索默认使用**当前已生效**的最新版本；未来生效的
版本不会提前改变结果。

## 运行

导入演练记录：

```
PYTHONPATH=src python -m nlp_curriculum_term_index.cli ingest fixtures/records.json
```

记录保存原始正文 `original_text` 和规范化正文 `text`；如果术语库已建立，`ingest`
还会为每条记录生成 `term_mentions`（词面、概念、教材版本、原文位置）并记录所用
术语表版本。

发布与更新术语表：

```
PYTHONPATH=src python -m nlp_curriculum_term_index.cli glossary-publish glossary.json --version-id v1
PYTHONPATH=src python -m nlp_curriculum_term_index.cli glossary-update patch.json --base-version v1
```

检索并保存快照：

```
PYTHONPATH=src python -m nlp_curriculum_term_index.cli search 分数 --subject 数学 --save
PYTHONPATH=src python -m nlp_curriculum_term_index.cli snapshot-list
PYTHONPATH=src python -m nlp_curriculum_term_index.cli snapshot-replay <snapshot-id>
```

`search` 不带 `--save` 时只返回结果，不写入归档。快照内嵌当次使用的完整术语表
版本与摘要；术语表更新不会改写已保存快照，重新查询才会采用当前有效版本。
`snapshot-replay` 会核对快照编号、术语表摘要，并列出命中原文与来源版本。
`--as-of` 可用当时生效的术语表版本重放历史查询。

运行数据默认保存在 `var/records.json`、`var/glossary.json`、`var/snapshots.json`。

## 测试

执行 `python -m pytest -q` 检查文本规范化、记录处理、状态读取、概念归属、教材
版本过滤、歧义保留和快照不变量。
