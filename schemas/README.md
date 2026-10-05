# schemas/ —— 产物契约（外置 JSON Schema）

四份首版契约，与代码里的真源**互为镜像**（改一边必须同时改另一边，`tests/test_schemas.py` 会红）：

| 文件 | 描述什么 | 代码里的真源 | 谁在跑它 |
|---|---|---|---|
| `shots.schema.json` | `shots.json` 结构 | `lvs/handoff.py`（`REQUIRED_FIELDS` / `VALID_SOURCES`） | G1 门禁判据（`lvs/criteria.py`）+ `lvs.schemas.validate("shots", …)` |
| `parse.schema.json` | `parse.json` 结构 | `lvs/parse.py` 的产出 | 契约测试（真实产物扫描） |
| `config.schema.json` | `config.toml` 的段与键 | `lvs/config.py` + 全仓 `config.get("a.b")` | `lvs config check` |
| `qc.schema.json` | 成片自检报告（`final-report.json`） | `lvs/qc_final.py` | `lvs qc --final` 自校验（测试中钉住） |

约定：

- **未知键只警告不算错**（schema 不开 `additionalProperties: false`）：代码里有动态键
  （`bgm.<key>` / `comfyui.<key>` / `[styles.<名字>]`），一刀切会误报；而"误报会让人不再信任校验器"。
- 校验走 `lvs.schemas.validate(kind, data)`（`jsonschema` 的 Draft 2020-12）。
- 契约里**只放"缺了会静默出错"的判据**，不做风格评判 —— 与 `handoff` 同一条原则。
