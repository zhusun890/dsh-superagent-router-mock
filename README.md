# dsh-superagent Router mock

[https://zhusun890.github.io/posts/dsh/](https://zhusun890.github.io/posts/dsh/)

```
uv sync
export DEEPSEEK_API_KEY=YOUR_KEY
```

```
uv run main.py --model deepseek-flash --scenario theme-followup --runs 1
```

```
uv run main.py --model deepseek-flash --scenario ambiguous-tickets --runs 3
```

```
uv run main.py --model deepseek-flash --scenario preference-storage --runs 3
```

```
uv run main.py --model deepseek-flash --scenario subagent-timeout --runs 3
```

---

用 [deepseek-ai/dsh-superagent-prototype](https://github.com/deepseek-ai/dsh-superagent-prototype)（commit `8c32a9d`）里 Router（代码里叫 orchestrator，也就是 main Session）的系统提示词、工具定义和消息格式，搭一个最小的交互环境，让一个模型扮演 Router，重放飞书群里的一串消息，看它怎么接单、挂单，以及会不会提醒 worker 几件事之间有关联。

## 场景

场景是 `scenarios/` 下的 JSON 文件，`uv run main.py --list-scenarios` 列出全部：

`theme-followup`：Alice 要亮色暗色开关，10 秒后没点回复、另发一条补充要求。看第二条是挂到第一个 ticket，还是另开一个。

`preference-storage`：Alice 要亮色暗色开关；6 分钟后 Bob 要把用户偏好从 localStorage 迁到后端。设定的前提是项目里的偏好都存在 localStorage，新开关的主题偏好也会这样存，所以两件事在偏好存储上有交集。

`ambiguous-tickets`：已经有三个进行中的 ticket（主题色抽成 CSS 变量、首页图表换新库、设置页亮色暗色开关），Alice 再发一条“暗色模式下首页图表的坐标轴文字看不清”。三个 ticket 都沾边；设定的前提是这个修复要用到暗色变量和新图表库的配置。

`subagent-timeout`：agent 框架仓库。Alice 要给 research subagent 加 60 秒超时、超时先返回已有结果；8 分钟后 Bob 要主 agent 汇总时提醒用户哪个 subagent 的结果不完整。设定的前提是 subagent 的返回值里现在没有“结果不完整”这个信号，Alice 的改动会第一次产生它。