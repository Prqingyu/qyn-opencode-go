"""apiusage —— AI API 用量采集与报告。

数据源（凭据与取数方式来自 example-org/DSH-PET 的逆向结果）:

* DeepSeek 官方余额: ``GET https://api.deepseek.com/user/balance``
* OpenCode GO 额度:  ``GET https://opencode.ai/console/api/go/status``
* OpenCode GO 明细:  ``GET https://opencode.ai/console/api/request-logs``

只用标准库（urllib），不引入第三方依赖，方便丢在中控机上 24×7 跑。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
