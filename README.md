# qyn-opencode-go

一个第三方的**额度面板/采集**小工具（`apiusage`）：用订阅账号的凭据去问服务商
"5 小时 / 本周 / 本月 各用了多少、还剩多少"，算好档位后输出一份结构化面板。

## 用法

```bash
cd apiusage
python -m apiusage panel          # 一行 JSON（给别的程序消费）
python -m apiusage panel --format text # 人看的表格
```

凭据只从环境变量/本地配置文件读取，**仓库里没有任何真实凭据**（示例值都是占位符）。

## 结构

| 路径 | 作用 |
|---|---|
| `apiusage/apiusage/providers/` | 各服务商的取数实现（`base.py` 定义契约） |
| `apiusage/apiusage/aggregate.py` | 把原始用量聚合成额度窗口 |
| `apiusage/gates.py` | 档位判定（`ok` / `warn` / `critical`） |
| `apiusage/panel.py` | 面板组装（schema 见 `apiusage/docs/INTERFACE.md`） |
| `apiusage/tests/` | 用假响应做的离线测试（不联网） |

## 许可

MIT（见 `LICENSE`）。
