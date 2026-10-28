# Agent Stuff

可复用的 agent skills 集合，当前包含适用于 Codex 工作流的七个 skill：

- `orchestrate`：跨项目协调 Codex 子代理，控制范围、生命周期、验证和失败边界。
- `commit`：创建简洁、规范的 Conventional Commits 提交。
- `session-health`：只读分析当前 Codex session 的健康度与上下文压力。
- `session-performance`：只读分析 Codex session 的性能、延迟、吞吐与近期趋势。
- `read-codex-sessions`：按明确的 session UUID 读取本地 Codex 会话问答。
- `github-weekly-summary`：先发现近期参与仓库，再逐分支查询过去 7 天的 GitHub 提交，按项目简洁总结工作成果。
- `compare-models`：通过 Artificial Analysis 比较模型价格、能力指数与任务成本。

## 查看可用 skill

以下本地命令均在仓库根目录执行。

```bash
npx -y skills add . --list
```

## 本地维护：推荐软链接

让仓库中的 `skills/<name>` 作为唯一源文件，在用户级目录 `~/.agents/skills/<name>` 建立软链接。修改本地文件或拉取仓库更新后，无需重新安装。Codex 官方支持该目录及软链接，并自动检测 skill 变化；未识别更新时重启 Codex。参见 [官方说明](https://learn.chatgpt.com/docs/build-skills)。

需要 Python 3。先预览，再执行所需安装命令：

```bash
# 只预览，不修改安装目录
python3 scripts/link_skills.py github-weekly-summary --dry-run

# 安装单个或多个 skill
python3 scripts/link_skills.py github-weekly-summary
python3 scripts/link_skills.py commit read-codex-sessions

# 需要全部 skill 时，显式选择 --all
python3 scripts/link_skills.py --all --dry-run
python3 scripts/link_skills.py --all
```

脚本自动定位仓库，无需写死 macOS 或 Linux 路径。同一个链接重复执行会显示 `UNCHANGED`；若发现同名文件、目录、不同目标链接或失效链接，会在创建任何链接前停止，不覆盖已有内容。`--dry-run` 也不会创建目录。用 `--target /path/to/project/.agents/skills` 可以改为项目级安装。

更新和移除：

```bash
# 获取远端更新；软链接本身不会自动拉取代码
git pull --ff-only

# 移除某个安装链接，保留仓库中的源文件
unlink "$HOME/.agents/skills/github-weekly-summary"
```

仓库路径需要保持稳定；移动仓库后应重新建立链接。新增 skill 也需要单独链接，或再次执行 `--all`。同一个 skill 只保留一处安装，避免与旧的 `~/.codex/skills` 或其他目录中的同名副本重复；本脚本不会自动迁移或删除旧安装。

## 使用 skills CLI 安装

不直接维护源文件时，也可使用 `skills` CLI。此方式与上面的仓库软链接方案择一使用；本仓库脚本提供的是直接指向当前 checkout 的链接。

安装单个 skill：

```bash
npx -y skills add . --skill commit
```

一次安装多个 skill：

```bash
npx -y skills add . \
  --skill orchestrate commit session-health session-performance read-codex-sessions github-weekly-summary compare-models
```

添加 `-g` 可以安装到用户级目录：

```bash
npx -y skills add . --skill commit -g
```

## GitHub 安装

从 GitHub 安装只包含远端已推送的内容，不包含本地未发布的修改。安装单个 skill：

```bash
npx -y skills add https://github.com/dwssef/agent-stuff.git --skill commit
```

或者一次安装全部 skill：

```bash
npx -y skills add https://github.com/dwssef/agent-stuff.git \
  --skill orchestrate commit session-health session-performance read-codex-sessions github-weekly-summary compare-models
```

只安装给 Codex 使用：

```bash
npx -y skills add https://github.com/dwssef/agent-stuff.git \
  --skill orchestrate \
  --agent codex \
  --yes
```

如需安装到 Codex 的用户级目录，可额外添加 `--global`：

```bash
npx -y skills add https://github.com/dwssef/agent-stuff.git \
  --skill orchestrate \
  --agent codex \
  --global \
  --yes
```

## 开发约定

每个 skill 位于 `skills/<skill-name>/`，入口文件必须是 `SKILL.md`。脚本、测试和 agent 元数据应与对应的 skill 一起维护。

软链接脚本的验证：`python3 -m unittest discover -s tests -v`。测试使用隔离临时目录，不写入真实安装目录。
