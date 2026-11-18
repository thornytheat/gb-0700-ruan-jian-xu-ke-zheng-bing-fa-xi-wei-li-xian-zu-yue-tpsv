# 接口

`POST /api/run`：{"text":"CSV内容"}。同规则版本与输入摘要只产生一条批次。

`GET/POST /api/1` 至 `/api/7`：独立样例或自定义文件计算。各模块返回 report、quarantine、balance。

`GET/POST /api/8`：共享席位池裁决。GET 载入 `sample_data/pool.csv` 样例；POST 接收 {"text":"席位池CSV"}，返回持久化批次（id、digest、result）。result.report.rows 为每个借用号的最新版本，borrow_history 为含 SUPERSEDED 的全版本，segments 为逐池逐部门占席段，audit 为收回/到期轨迹。

`POST /api/8/preview`：同入参但不落库，返回纯重放结果。`GET /api/8/runs` 列出席位池批次，`GET /api/8/runs/{id}` 取详情。同规则版本与输入摘要复用同一批次，重跑不新增借用、不重复占席。

`POST /api/review`：id、整数revision、decision取通过或退回。旧版本409；阻断项不能通过。

`GET /api/export/{id}`：唯一证据CSV。`GET /health`：存活检查。

错误：结构/数值422，版本冲突409，不存在404，文件过大413。逐行异常进入quarantine。
