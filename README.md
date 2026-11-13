# 我的工作流

把 AI 短剧的**剧本与资产 → 自动生成 → 查错与剪辑 → 规则复用**装进一个 Skill。你继续使用现有 AI 助手，工作台负责把项目和产物放在一起。

**纯 HTML 工作台，无后台服务、无 Node.js 依赖。** 自动执行由助手调用 Python 工具；生成使用你自己的 ComfyUI 和工作流。安装包不附带模型、算力或聊天 API。

## 开始使用

把本仓库交给支持本地工具的 AI 助手：

> 请安装 https://github.com/yhaolpz/my-workflow 这个 Skill，阅读 SKILL.md，然后启动“我的工作流”工作台。

Codex 可使用 Skill 安装器。WorkBuddy 可下载仓库发布的 ZIP，通过“添加技能 → 上传技能”导入。不同助手的安装方式以其实际能力为准；普通网页聊天可以使用模板，自动执行需要读写文件和运行工具。

也可下载后直接启动：

```sh
python scripts/workflow.py init --open
```

Windows 可将 `python` 改为 `py -3`。默认数据目录是 `~/MyWorkflow`；指定其他位置：

```sh
python scripts/workflow.py init --workspace "/你的工作目录" --open
```

首次在页面点“连接工作目录”，选择该文件夹。配置、项目和规则保存到 AI 可直接读取的 `workspace.json`。Chrome/Edge 支持直接目录保存；其他浏览器使用 JSON 导入导出。个人数据与安装包分开，更新不会覆盖。

## 工作台里有什么

- 剧目库：每部剧分别保存故事、资产、分镜、任务和审片记录。
- 四环节模板：每阶段复制对应项目提示词，回到正在用的 AI 助手执行。
- 生成配置：自己的 ComfyUI 地址、API 工作流、文戏与动作预设、节点槽位和预算。
- 素材预览：人物、场景、参考图和生成片段；镜头提示词可完整查看和复制。
- 规则库：查看 Skill 方法，编辑个人品味和避坑经验，区分项目和全局。
- 官方指南：保留 H3 官方入口，按模式刷新缓存，记录来源与哈希。

## 连接自己的生成链路

名称不能代替工作流。请在 ComfyUI 中导出 **API 格式 JSON**，导入后选择提示词、时长和种子输入；文戏与动作可用不同图或不同参数覆盖。助手运行 `doctor`、`preflight` 后，按授权范围执行 `run --execute`。

已提交任务保留 ID，避免超时后重复扣算力。下载不自动等于审片通过；检查后再登记取用范围。任务数、生成总秒数、重做次数均有上限。

基础拼接与媒体检查需要 FFmpeg/ffprobe。若选择 Remotion，助手另外建立可编辑工程并检查它的运行环境；工作台本身仍是静态页面。

详见 [快速开始](references/quickstart.md)、[ComfyUI 配置](references/comfyui.md)、[H3 提示词](references/h3-prompts.md)、[制作与审片](references/production.md)。

## 验证

```sh
python -m unittest discover -s tests -v
```

测试使用本地模拟服务验证 API 逻辑，不会提交真实 GPU 任务。实际模型效果仍需在自己的节点、权重和素材上验收。版本与验证范围见 [实现说明](docs/architecture.md)。

MIT License。第三方模型、工作流及其许可由各自提供者决定。
