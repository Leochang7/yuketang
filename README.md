# YuKeTang

一个面向雨课堂的 Python 自动化项目，当前支持扫码登录、课件处理、讨论题评论、测试题处理、异步视频/课件补刷，以及测试题只收集不作答。

## 项目来源

本项目基于以下仓库继续开发和调整：

- Upstream: `https://github.com/Zachary709/YuKeTang`

在此基础上，当前仓库做了结构拆分、多课程支持、异步入口补齐、视频扫描后补刷、题目收集模式、编码与日志修复等调整。

## 当前能力

### 登录

- 优先复用本地 `cookies.json`
- 本地登录失效时，自动进入 WebSocket 扫码登录流程

### 视频

- 支持多课程处理
- 支持异步入口处理视频和课件
- 视频处理会先扫描状态，再集中补刷未完成视频
- 会结合 `video-log/detail` 的覆盖区间做缺口补刷
- 支持普通模式和快速模式

### 课件

- 支持 `1/2/3/5` 类型课件
- 可以单独处理，也可以和视频一起处理
- 并发模式下默认先处理课件，再处理视频

### 讨论题评论

- 支持按课程批量处理
- 优先处理未完成内容
- 支持固定评论内容或 LLM 生成评论

### 测试题

- 支持单选、多选、判断、填空
- 支持本地答案优先，LLM 兜底
- 支持按得分情况过滤，尽量只处理未得分测试

### 测试题收集

- 支持只收集题目，不作答
- 支持多课程批量导出
- 按课程分别输出到 `questions/` 目录
- 导出内容只包含题干、题型和选项，不会调用本地答案、LLM 或提交接口

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

1. 安装依赖
2. 按需修改 [config.yml](/D:/workspace/YuKeTang/config.yml)
3. 运行

```powershell
python course_app.py
```

4. 首次登录时扫码，后续默认复用本地 cookies

## 当前菜单

运行后当前菜单如下：

- `1` 自动刷讨论题评论
- `2` 自动刷测试题
- `3` 查看/完成课件
- `4` 异步刷视频和课件
- `5` 收集测试题（不作答）
- `0` 退出

说明：

- 菜单 `1` 到 `6` 都支持多课程选择
- 菜单 `4` 是“先课件、后视频”
- 菜单 `4` 本质上是“异步调度 + 线程执行 worker”
- 菜单 `5` 只收集题目，不作答

## 模式说明

### 异步刷视频和课件

- 会询问异步并发数
- 会询问是否使用快速模式
- 本质上是异步调度壳，底层视频和课件执行仍复用已有 worker
- 当前是项目里保留的唯一视频/课件并发入口

### 收集测试题

- 会先让你选择一个或多个课程
- 每门课程单独导出一个文本文件
- 输出目录是 [questions/](/D:/workspace/YuKeTang/questions)
- 不会提交答案，也不会调用 LLM

## 输出目录

### `answer/`

- 本地答案文件目录
- 测试题自动作答时会优先读取这里的课程答案

### `questions/`

- 测试题收集输出目录
- 每门课程一个 `.txt` 文件
- 仅包含题目，不包含答案

### `logs/`

- 运行日志目录
- 具体是否生成取决于你当前本地使用方式和调试开关

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
  app/                 程序入口
  auth/                登录与 cookies 管理
  core/                视频、课件、讨论题、测试题等核心逻辑
  llm/                 LLM 调用封装
  network/             同步/异步 HTTP 封装
  utils/               日志、配置、字体解析、答案解析等工具
answer/                本地答案文件
questions/             测试题导出目录
config.yml             配置文件
course_app.py          根入口
```

## Git

当前目录已经重新初始化为新的 Git 仓库，默认分支为 `main`。  
`.gitignore` 已覆盖常见本地文件和运行产物，见 [.gitignore](/D:/workspace/YuKeTang/.gitignore)。

## 注意事项

- 本项目依赖雨课堂在线接口，接口返回可能随平台变化
- 视频“覆盖率”和表面播放进度不是一回事，应以服务端明细为准
- 高并发和快速模式都可能提高被平台识别异常的风险
- 测试题、评论和题目收集功能都涉及真实课程内容，使用前请自行评估风险

## 声明

仅供学习和研究，请勿用于违反学校规定、服务条款或法律法规的用途。
