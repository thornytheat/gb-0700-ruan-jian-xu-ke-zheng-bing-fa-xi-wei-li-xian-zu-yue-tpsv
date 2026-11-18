# 席位定义

lease_id、user保持字符串。CAD-Pro、cad归一为CAD。kind取online、offline、heartbeat、release、capacity。start/end必须带时区并转换UTC。seats为正整数；capacity记录表示采购生效区间的席位上限。

在线租约可由同租约同版本release提前结束；离线授权一直占用至原到期时刻，即使收到release仍保留。心跳不建立租约。租约采用最高版本；同版多条实体租约冲突隔离。采购区间可重叠，最高version优先，同最高版不同数量返回CAPACITY_CONFLICT。

并发使用半开区间[a,b)，结束同刻开始不重叠。采购边界必须切分并发段。超用责任按租约开始时间倒序、再按编号倒序分配，分配席位严格等于超用数量。超用席位小时为超用数量×秒数/3600，Decimal计算。此工具只核验约定席位数，不含厂商复杂许可证条款。

租约ID在产品内唯一；不同产品允许相同租约号。线上租约在start同刻release得到零时占用，离线仍保留原到期。

# 共享席位池（M8）

输入字段：event_id、pool、dept、kind、start、end、version、seats、ref。event_id 保持字符串，同一池内重复事件号按 DUPLICATE_EVENT 隔离；时间须带时区并转换UTC。

kind 取 quota、borrow、return、recall、session：

- quota：部门在某席位池的固定配额，按 start 时刻生效，同部门后到高版本覆盖当前值；seats 为非负整数。
- borrow：dept 为出借方，ref 为借入方，[start,end) 为借用区间，seats 为借用席位数。出借方当时空闲席（生效配额减去未归还借出）不足时 BORROW_NO_IDLE 隔离；出借方尚无配额 QUOTA_NOT_FOUND；自借 BORROW_SELF。
- return：借入方提前归还，ref 填借用号；recall：出借方临时收回，ref 填借用号。两者 version 必须高于借用当前版本，否则 STALE_VERSION 隔离；引用不存在的借用号 BORROW_NOT_FOUND；发生在借用到期之后只记 TERMINATION_LAPSED 审计，不改写历史。
- session：部门生产会话区间。recall 发生时刻若借入方存在进行中的会话（session.start < recall.start < session.end），收回顺延到最后一个进行中会话的结束时刻，且不越过原借用到期；无进行中会话则即时收回。

每条借用关系只追加、不改写。borrow 生成 rev=1 记录，归还/收回追加新版本，旧版本标记 SUPERSEDED 原样保留，其原到期安排不变。记录含 borrow_id、pool、rev、lender、borrower、seats、origin_quota（出借当时的固定配额快照）、start、planned_return（原到期）、actual_return（实际归还/收回/到期）、requested_at、reason、status。reason 取 BORROW_GRANTED、RETURNED、RECLAIMED、RECALL_DEFERRED；status 取 EFFECTIVE、COMPLETED、SUPERSEDED，自然到期只把状态置 COMPLETED 并在审计记 EXPIRED，记录 reason 不变。

占席账按半开区间 [a,b) 切分，逐池逐部门输出 quota、lent、borrowed、available。重放为纯函数，同批事件重复提交得到相同借用关系与占席段；持久化按规则版本加输入摘要去重，不新增借用、不重复占席。
