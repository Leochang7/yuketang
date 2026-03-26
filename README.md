# YuKeTang

一个面向雨课堂的 Python 自动化项目，当前支持扫码登录、视频覆盖率补刷、课件完成、讨论题评论、测试题处理，以及多课程、多线程、异步调度。

## 项目来源

本项目基于以下仓库继续开发和调整：

- Upstream: `https://github.com/Zachary709/YuKeTang`

在此基础上，当前仓库做了结构拆分、异步入口补齐、多课程支持、视频扫描后补刷、编码与日志修复等改动。

## 当前能力

- 登录
  - 复用本地 `cookies.json`
  - 登录失效时自动进入 WebSocket 扫码登录流程
- 视频
  - 支持单课程和多课程
  - 支持普通模式、快速模式
  - 先统一扫描视频状态，再只补刷覆盖率未达 `100%` 的视频
  - 对状态不稳定的视频会做更强探测后再纳入补刷队列
  - 根据 `video-log/detail` 的覆盖区间定向补未覆盖片段
- 课件
  - 支持 `1/2/3/5` 类型课件
  - 可单独处理，也可与视频一起处理
- 讨论题评论
  - 只处理未得分讨论题
  - 可使用固定评论模板或 LLM 生成评论
- 测试题
  - 支持单选、多选、判断、填空
  - 支持本地答案优先，LLM 兜底
  - 尽量只处理未得分测试题

## 运行环境

- Python 3.10+
- Windows PowerShell 或其他支持 UTF-8 的终端

安装依赖：

```powershell
pip install -r requirements.txt
```

当前依赖见 [requirements.txt](/D:/workspace/YuKeTang/requirements.txt)：

- `aiohttp`
- `ddddocr`
- `pillow`
- `requests`
- `openai`
- `pydantic`

## 快速开始

1. 安装依赖。
2. 按需修改 [config.yml](/D:/workspace/YuKeTang/config.yml)。
3. 运行：

```powershell
python course_app.py
```

4. 首次登录时扫码，后续默认复用本地 cookies。

## 菜单说明

- `1` 自动刷视频
- `2` 自动刷讨论题评论
- `3` 自动刷测试题
- `4` 查看/完成课件
- `5` 多线程刷视频和课件
- `6` 异步刷视频和课件

说明：

- 菜单 `1` 到 `6` 都支持多课程选择
- 菜单 `5` 和 `6` 的顺序是先处理课件，再处理视频
- 视频处理会先扫描，再集中补刷未完成视频
- 异步模式本质是异步调度加线程执行视频/课件 worker

## 配置说明

[config.yml](/D:/workspace/YuKeTang/config.yml) 常用项包括：

- `default_comment`
  - 固定评论内容
  - 设为 `None` 时改为 LLM 生成
- `DASHSCOPE_API_KEY`
  - 兼容 OpenAI 接口的模型密钥
- `LLM_BASE_URL`
  - 模型服务地址
- `LLM_MODEL`
  - 使用的模型名
- `HTTP_DEBUG`
  - 是否打印 HTTP 请求日志
- `HTTP_DEBUG_DETAIL`
  - 是否打印关键接口摘要

## 项目结构

```text
src/
  app/                 入口
  auth/                登录与 cookies
  core/                视频、课件、讨论题、测试题主逻辑
  network/             同步/异步 HTTP 封装
  utils/               日志、配置、字体解析、答案解析等工具
answer/                本地答案文件
config.yml             配置文件
course_app.py          根入口
```

## Git

当前目录已经重新初始化为新的 Git 仓库，默认分支为 `main`。  
`.gitignore` 已覆盖常见本地文件和运行产物，见 [.gitignore](/D:/workspace/YuKeTang/.gitignore)。

## 注意事项

- 本项目依赖雨课堂在线接口，接口返回可能随平台变化。
- 视频“覆盖率”和表面播放进度不是一回事，应以服务端明细为准。
- 测试题与评论功能涉及真实课程内容，使用前请自行评估风险。

## 声明

仅供学习和研究，请勿用于违反学校规定、服务条款或法律法规的用途。
