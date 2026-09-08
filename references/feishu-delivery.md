# 飞书交付与版本替换

仅在用户指定飞书或当前工作区默认飞书交付时使用。用户选择本地文件、其他平台或只做样片时，按其范围执行。先读取当前可用的 `lark-doc` 技能和相应 create/update/media-insert 参考；接口有变化时以本机帮助为准。

## 准备成片后再写交付页

本地完成渲染、字幕校对和媒体核查，运行打包脚本，确认包内文件与当前版本一致。不要把只有文件名的占位文本描述为视频已上传。

交付页可包含简短版本说明、逐条标题、原声/案例来源属性、发布文案、原片区间、单条预览、封面和整包下载。总条数、时长、文件大小来自实际产物。`package_delivery.py` 生成的 XML 可作正文起点。

每条使用唯一文字锚点，例如 `成片 01｜名称`、`封面 01｜名称`；完整素材包使用单独锚点。

## CLI 要点

```bash
lark-cli docs +fetch --doc <doc-id> --detail with-ids
lark-cli docs +create --content @tmp/task/delivery.xml --as user
lark-cli docs +media-insert --doc <doc-id> --file outputs/task/01_name.mp4 --type file --file-view preview --selection-with-ellipsis '成片 01｜名称' --as user
lark-cli docs +media-insert --doc <doc-id> --file outputs/task/封面/01_name.jpg --type image --width 360 --selection-with-ellipsis '封面 01｜名称' --as user
```

`--file` 使用当前工作目录内的相对路径。工具调用使用独立参数或结构化参数，避免把标题、文案和路径拼接成未经引用的 shell 命令。多行 XML 写文件后用 `@相对路径` 传入。

## 更新已有交付页

1. 读取当前文档内容与相关 block ID，保存本次变更涉及的旧媒体标识。
2. 按原编号追加新案例或更新必要文案，保留用户已有内容。先上传新版视频、封面和素材包，记录返回的 file token、block ID、源文件 SHA256 与大小。
3. 同一文档的写操作串行执行。若响应不明确，先 fetch 核查上传结果，再决定补写，避免重复插入。
4. 新版文件确认可用后，再删除本次替换范围内的旧媒体块。删除前重新读取，核对 ID；文档块的删除与源文件删除分开处理。
5. 最后更新标题、版本、条数和总时长，重新 fetch 验证：文件名唯一、数量正确、视频是 Preview、上传 token 与日志一致、远端文件大小匹配本地文件。

重试按文件内容识别版本，不能只凭一个历史“上传成功”日志跳过已改变的文件。确认无结果后可重试一次；相同权限/配额错误持续出现时停止该写入，保存本地成果并说明具体原因，避免循环重试。

最终优先提供飞书链接，并附可用本地素材包。若工作区维护文档历史索引，完成后按其规则追加链接。技能本身不保存登录凭证、浏览器 cookie、固定文档 ID 或某次会议的源文件。
