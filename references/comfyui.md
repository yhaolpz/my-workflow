# 连接和工作流预设

在设置中填写本地或远程 ComfyUI 基础地址，例如 `http://127.0.0.1:8188`。认证需要 Bearer token 时只填环境变量名，把令牌留在助手的运行环境。不要把 Cookie、账号密码或带密钥的 URL 写进配置。

工作流名称只是便于识别的标签。打开自己真正要用的 ComfyUI 工作流，导出 API 格式 JSON，再导入工作台。API 图的每个节点都有 `class_type` 和 `inputs`；含 `nodes` 的画布 JSON 需要先在 ComfyUI 中另行导出，不能直接提交。

文戏和动作是两个预设，可以导入不同 JSON，也可使用同一张图并指定不同的节点参数覆盖。选择实际的提示词、时长和 seed 槽位；参考素材按“节点 ID.输入名 → 资产 ID”绑定。覆盖参数也使用同一槽位格式，例如 `12.strength_model`。不要把特定作者的 LoRA 文件名当成每个用户都已安装的依赖。

运行 `doctor` 检查连接和工具；运行 `preflight --project 项目ID` 检查实际节点、必填参数、连接和枚举值。未映射时长就沿用原图真实时长，预检要求镜头时长和原图一致；不要只在提示词里写十秒而让节点仍生成五秒。

生成需显式 `run --project 项目ID --execute`。任务指纹避免同一镜重复提交；POST 出错或超时会记录 `submission_unknown`，需要查原任务并确认，不自动重发。排队、执行、下载、技术检查、视审和用户验收是不同状态。

API 依据：[ComfyUI Server Routes](https://docs.comfy.org/development/comfyui-server/comms_routes)。工作台不从浏览器直接调用云端 API，助手通过本地 Python 客户端调用，避免依赖页面跨域设置。现阶段支持普通 API 节点图，专有代理认证和界面子图需按实际环境适配。
