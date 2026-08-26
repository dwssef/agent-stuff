# Agent Stuff

可复用的 agent skills 集合，当前包含适用于 Codex 工作流的三个 skill：

- `commit`：创建简洁、规范的 Conventional Commits 提交。
- `session-health`：只读分析当前 Codex session 的健康度与上下文压力。
- `session-performance`：只读分析 Codex session 的性能、延迟、吞吐与近期趋势。
- `read-codex-sessions`：按明确的 session UUID 读取本地 Codex 会话问答。

## 查看可用 skill

```bash
npx -y skills add /home/czy/p/agent-stuff --list
```

## 本地安装

安装单个 skill：

```bash
npx -y skills add /home/czy/p/agent-stuff --skill commit
```

一次安装多个 skill：

```bash
npx -y skills add /home/czy/p/agent-stuff \
  --skill commit session-health session-performance read-codex-sessions
```

添加 `-g` 可以安装到用户级目录：

```bash
npx -y skills add /home/czy/p/agent-stuff --skill commit -g
```

## GitHub 安装

仓库发布到 GitHub 后，可使用以下命令安装单个 skill：

```bash
npx -y skills add https://github.com/dwssef/agent-stuff.git --skill commit
```

或者一次安装全部 skill：

```bash
npx -y skills add https://github.com/dwssef/agent-stuff.git \
  --skill commit session-health session-performance read-codex-sessions
```

只安装给 Codex 使用：

```bash
npx -y skills add https://github.com/dwssef/agent-stuff.git \
  --skill commit \
  --agent codex \
  --yes
```

如需安装到 Codex 的用户级目录，可额外添加 `--global`：

```bash
npx -y skills add https://github.com/dwssef/agent-stuff.git \
  --skill commit \
  --agent codex \
  --global \
  --yes
```

## Codex 专属开发安装

`skills` CLI 会始终将 skill 安装到通用目录 `.agents/skills`。`--agent codex` 可以避免额外安装到 Claude Code 等 agent 目录，但不会改为使用 Codex 专属目录。

如果你在本地维护这个仓库，并希望 Codex 中的修改能被 Git 直接感知，应让仓库作为唯一源文件，再将 Codex 用户目录链接到仓库：

```bash
repo_root=/home/czy/p/agent-stuff
codex_skill_root="$HOME/.codex/skills"
mkdir -p "$codex_skill_root"

for skill in commit session-health session-performance read-codex-sessions; do
  repo_skill="$repo_root/skills/$skill"
  codex_skill="$codex_skill_root/$skill"
  test -d "$repo_skill"
  test ! -e "$codex_skill" && test ! -L "$codex_skill"
  ln -s "$repo_skill" "$codex_skill"
done
```

之后 Codex 读取和修改的实际文件都位于仓库中，`git status` 会直接显示 skill 改动。普通使用者仍可使用上面的 `npx skills add` 命令安装发布版本。

## 开发约定

每个 skill 位于 `skills/<skill-name>/`，入口文件必须是 `SKILL.md`。脚本、测试和 agent 元数据应与对应的 skill 一起维护。
