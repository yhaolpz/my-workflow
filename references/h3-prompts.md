# MiniMax H3 提示词

官方入口：[h3-prompt-writing](https://github.com/MiniMax-AI/MiniMax-H3/blob/main/skills/h3-prompt-writing/SKILL.md)。写提示词前运行 `guide refresh`，把官方入口和它引用的 `references/base-en.txt`、`references/ref-en.txt` 缓存到工作目录；每份记录下载时间、来源和 SHA256。刷新失败保留上次成功文件，不以空内容替换。

2026-11-13核对的结构：

| 输入模式 | 描述结构 |
|---|---|
| T2VA、I2VA、FL2VA、L2VA | `integrated_multimodal_description`、`overall_soundscape`、`non_diegetic_music` |
| Ref2VA 全参考 | `subject_definitions`、`summary`、`retention_analysis`、`detailed_description`、`overall_soundscape`、`non_diegetic_music` |

描述用英文，准确台词、歌词和需要出现的画面字保留原语言。不要把中文镜头标题或梗概混进生成描述，防止被误念成台词。按实际上传顺序绑定图片、音频、人物和说话人编号；身份参考、精确首尾帧和音色参考的含义不同。

逐时间段说明构图、人物、场地、动作、相机、声音以及结束状态。首次独立十秒是本工作流的编排起点，不是模型固定限制；补拍按真实需要，节点范围与帧对齐以当前实例为准。官方内容更新后优先读取最新缓存，适用的个人规则仍保留；指南不能替代实际产物的视审。
