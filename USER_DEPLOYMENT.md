# 个人短线量化部署

本分支保留 Quanti 的完整量化、回测、模拟盘和 Web 能力，并增加适合 GitHub Actions 的轻量入口：

- 工作日 08:20、10:30、14:30（北京时间）扫描 A 股；
- 量价打分，过滤 ST/退市、北交所、明显追高、长上影、低流动性股票；
- 生成 7–10 个候选，其中前 3–5 个为主推；
- QQ 邮箱发送日报，企业微信仅推主推和可操作风控信号；
- 从“确认模拟交易”Actions 手动确认后，才写入 10 万元模拟账本；
- 模拟成交计入 0.1% 单边滑点，最多同时持有 3 只；
- 分段止损和止盈按 `config/short_term.json` 执行。

所需仓库配置：变量 `EMAIL_SENDER`、`LLM_PRIMARY_BASE_URL`、`LLM_PRIMARY_MODEL`，Secrets `EMAIL_PASSWORD`、`WECOM_WEBHOOK_URL`、`LLM_PRIMARY_API_KEY`。同花顺问财因当前要求登录 Cookie，可选增加 Secret `WENCAI_COOKIE`；不配置时系统仍用腾讯/新浪/东方财富多源行情运行。

看板由 `docs/quant/index.html` 提供，可直接使用 GitHub Pages；需要 Cloudflare Pages 时，将构建目录设为 `docs/quant`，无需额外构建命令。
