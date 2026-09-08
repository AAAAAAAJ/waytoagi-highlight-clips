# WaytoAGI 高光剪辑 Skill

把飞书妙记、共学直播或本地长视频制作成适合二次传播的高光短视频。包含案例筛选、口癖与冗余片段精剪、前 3 秒设计、原声与字幕同步、WaytoAGI 品牌排版、渲染、验收和交付流程。

## 适合什么任务

- 从长视频中筛选实际案例、操作方法或有趣原话，制作可独立理解的短视频。
- 给已有成片追加案例、调整背景样式或优化前 3 秒。
- 剪掉无意义口癖、重复起句、同义复述和无关等待，让原声表达更紧凑。
- 用同次演示的真实结果画面开场，后文保留对应操作、结论和适用边界。
- 批量导出 MP4、封面、SRT 字幕、发布文案、剪辑时间码与 ZIP 素材包。

默认画布为 1080×1920、25 fps，H.264/AAC。视觉模板采用灰白背景、黑色大字、彩色块、原版 WaytoAGI Logo 黑色页眉。标题、来源、演示画面、字幕和看点分别排版。

## 作为 Skill 使用

下载仓库，保留完整目录结构：

```bash
git clone https://github.com/AAAAAAAJ/waytoagi-highlight-clips.git
```

将整个文件夹放入使用工具的技能目录。Codex 用户可放到 `~/.codex/skills/waytoagi-highlight-clips/`；已有同名技能时先确认版本，保留自己的修改。

在任务中调用：

```text
使用 $waytoagi-highlight-clips，把这段妙记剪成适合二次传播的高光短视频。
沿用当前品牌样式，去掉无意义口癖和冗余片段，前3秒先讲清这是什么、能做什么或在讲什么。
来源：填入本次妙记链接或本地视频路径。
```

AI 助手按 [SKILL.md](SKILL.md) 执行，并结合实际源视频确定剪点、字幕和案例归属。飞书获取与上传使用运行环境中已安装、已授权的飞书工具；本仓库提供剪辑与交付流程说明。

## 直接运行脚本

需要 Python 3.10+、Pillow、FFmpeg 和 ffprobe。FFmpeg 需要支持 `libx264`、AAC、`ass`（libass）与 `loudnorm`。准备可显示中文的本地字体，系统字体不随仓库分发。

在仓库目录创建 Python 环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
ffmpeg -version
ffprobe -version
```

创建独立项目配置：

```bash
mkdir -p projects/demo
cp assets/project.example.json projects/demo/project.json
```

将源视频放到 `projects/demo/source.mp4`，或在 JSON 的 `source` 中填写实际路径。填写本次日期、讲者、标题、裁切、字幕及保留区间。示例里的日期与 8 秒区间用于说明结构，须按实际素材替换。

所有相对路径均以项目 JSON 所在目录为基准。详细字段见 [项目配置说明](references/project-schema.md)。

```bash
# 检查输入，再生成前8秒样片
python scripts/render_clips.py --project projects/demo/project.json --validate-only
python scripts/render_clips.py --project projects/demo/project.json --clip 01 --sample 8

# 导出正式成片、字幕和封面
python scripts/render_clips.py --project projects/demo/project.json

# 技术验收；自定义帧率时同步传入 --fps
python scripts/verify_media.py projects/demo/output --report-dir projects/demo/work/verify

# 生成发布文案、时间码、ZIP和清单
python scripts/package_delivery.py --project projects/demo/project.json
```

样片保存在 `work_dir/samples/`，正式文件保存在 `output_dir/`。技术验收覆盖解码、编码、尺寸、帧率、音画时长差与音频峰值；交付前仍需检查原声、字幕、开头承接及来源署名。

脚本按项目 JSON 渲染。下载视频、转写、选题和发布操作由使用 Skill 的助手结合可用工具完成。

## 去口癖与删冗余片段

由助手结合逐字稿、原音和画面判断哪些内容可删。支持填充音、重复词、卡顿起句、冗余复述与无关等待；有语义的连接词、否定、数量、条件、情绪和演示音效需要保留。

先按 [精剪说明](references/speech-cleanup.md) 写出基于旧成片时间的删除计划，再生成新项目：

```bash
python scripts/refine_speech.py --project projects/demo/project.json --edits projects/demo/speech-edits.json --out projects/demo-clean/project.json
python scripts/render_clips.py --project projects/demo-clean/project.json --validate-only
python scripts/render_clips.py --project projects/demo-clean/project.json
python scripts/verify_media.py projects/demo-clean/output --report-dir projects/demo-clean/work/verify
python scripts/package_delivery.py --project projects/demo-clean/project.json
```

`refine_speech.py` 应用已复核的删除决定，更新声音与画面的保留区间，并同步调整字幕、补画面、来源标签、封面与开头标题时间。它保留旧项目，生成新的项目与 `speech-edit-report.json`；实际裁音由后续渲染完成。不能用只删字幕或全片加速来代替精剪。

## 前 3 秒与剪辑规则

- 前 3 秒先说明“具体对象或内容类型 + 功能或主题”，如“阅读笔记工具｜点笔记跳回原文”。用清楚的主题和真实画面吸引观众。
- 开头只承诺一个具体看点：真实结果、可验证的问题、完整原话或明确的交互变化。
- 编辑标题与原声字幕分区，字幕跟随实际说话内容。
- `keep` 按数组顺序拼接，支持精彩原话前置及重复片段；字幕按最终时间轴重新映射。
- 补充画面来自同次演示，冻结帧会显示“画面定格”。
- 保留本人实测、网上案例、模拟读数、已有插件等来源与限制。
- 静音只看前 3 秒也应能说清内容；精剪时保留必要介绍，剪后重新核对。
- 发布后结合留存数据迭代，避免承诺固定的完播提升。

## 文件说明

| 文件 | 用途 |
|---|---|
| [SKILL.md](SKILL.md) | 助手调用入口与完整流程 |
| [editorial-workflow.md](references/editorial-workflow.md) | 案例筛选、校对与剪辑 |
| [speech-cleanup.md](references/speech-cleanup.md) | 去口癖、删冗余、原音剪点与时间轴调整 |
| [opening-hooks.md](references/opening-hooks.md) | 开头设计与后文兑现 |
| [brand-style.md](references/brand-style.md) | 颜色、排版、字体与 Logo 规范 |
| [project-schema.md](references/project-schema.md) | 项目 JSON 字段和时间轴规则 |
| [feishu-delivery.md](references/feishu-delivery.md) | 飞书预览、素材包和版本替换 |
| `scripts/refine_speech.py` | 应用删除计划并同步调整项目时间轴 |
| `scripts/render_clips.py` | 样片与正式视频渲染 |
| `scripts/verify_media.py` | 媒体技术验收 |
| `scripts/package_delivery.py` | 发布文案、时间码与素材包 |
| `assets/` | 原版 Logo 和项目配置模板 |

Python 依赖固定为本次验证使用的 Pillow 版本。已用真实源视频验证样片、正式渲染、字幕重排和打包；案例素材与中间记录保存在各自项目目录。
