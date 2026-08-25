# Agent Stuff

可复用的 agent skills 集合，当前包含适用于 Codex 工作流的三个 skill：

- `commit`：创建简洁、规范的 Conventional Commits 提交。
- `session-health`：只读分析当前 Codex session 的健康度与上下文压力。
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
  --skill commit session-health read-codex-sessions
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
  --skill commit session-health read-codex-sessions
```

## 开发约定

每个 skill 位于 `skills/<skill-name>/`，入口文件必须是 `SKILL.md`。脚本、测试和 agent 元数据应与对应的 skill 一起维护。
