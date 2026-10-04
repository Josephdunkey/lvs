# Triage Labels

The skills speak in terms of five canonical triage roles. This file maps those roles to the actual label strings used in this repo's issue tracker.

| Label in mattpocock/skills | Label in our tracker | Meaning                                  |
| -------------------------- | -------------------- | ---------------------------------------- |
| `needs-triage`             | `needs-triage`       | Maintainer needs to evaluate this issue  |
| `needs-info`               | `needs-info`         | Waiting on reporter for more information |
| `ready-for-agent`          | `ready-for-agent`    | Fully specified, ready for an AFK agent  |
| `ready-for-human`          | `ready-for-human`    | Requires human implementation            |
| `wontfix`                  | `wontfix`            | Will not be actioned                     |

When a skill mentions a role (e.g. "apply the AFK-ready triage label"), use the corresponding label string from this table.

## Lifecycle status（本仓库的票还有后半段）

上面那五个是**分诊**角色（票刚进来时用）。票被接手之后走的是生命周期状态，
写在同一行 `Status:` 里：

| Label in our tracker | Meaning |
| -------------------- | ------- |
| `claimed`            | 决策类票已被接手（见 `issue-tracker.md` 的 wayfinder 约定） |
| `resolved`           | 决策类票已给出答案，答案写在 `## Answer` 下 |
| `ready-for-agent`    | 规格完整、可交给无人值守的 agent 实现 |
| `ready-for-human`    | 需要人来做（要人的判断、外部服务、一次性的手动步骤） |
| `done`               | 已实现 + 测试通过（有可能被真机验证过的部分另记在票里） |
| `open`               | 记录着、还没定怎么做的设计问题（**尽量改用 `ready-for-human`**，别用它当默认） |
| `wontfix`            | 明确不做 |

一条纪律：**`done` 必须能被代码与前一轮的审查记录对上**。票里声称的行为、验收框、
以及"现场验证"段落如果和代码不符，宁可标 `ready-for-human` 并写清差在哪 —— 这比一个
漂亮的 `done` 有用（五轮审查里最值钱的发现，全都是"票说 done 但代码不是"。）

Edit the right-hand column to match whatever vocabulary you actually use.
