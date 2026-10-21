# 麦当劳 MCP 集成说明

## 使用的 MCP Server

| 项 | 值 |
|---|---|
| Server | 麦当劳中国 MCP Server |
| 类型 | streamable HTTP |
| 端点 | `https://mcp.mcd.cn` |
| 认证 | `Authorization: Bearer $MCD_MCP_TOKEN`（[open.mcd.cn](https://open.mcd.cn) 手机号登录后在控制台激活获取） |
| 协议 | JSON-RPC 2.0（`initialize` → `tools/call`），服务端暴露 35 个 tool |

本项目用标准库 `urllib` 直连，不依赖任何 MCP SDK，也不依赖第三方包。实现见
[`maxout/client.py`](maxout/client.py)：优先读取响应里的 `structuredContent`，
退化时从 Markdown 文本的 `## Original Response` 之后解析真 JSON。

## 用到的 Tool 与调用流程

```
① query-nearby-stores   ← 可选：给城市+地标时，先定位门店拿 storeCode
        ↓
② query-meals           ← 拿整份菜单：104 个 SKU 的 currentPrice / originalPrice / tags
        ↓
③ query-meal-detail     ← 逐个套餐拿 rounds（蓝区/粉区、每个选项的 diffPrice）
        ↓                   一次运行约 44 次调用，展开出 16,902 个套餐变体
④ query-store-coupons   ← 当前门店可用券；为空则退回 query-my-coupons 卡包
        ↓                   都没有时用 available-coupons（可领券）做上限预演
⑤ 本地求解（DP + 券子集）← 去重后 9,212 个购买单元 + 若干一次性券单元，精确整数规划
        ↓
⑥ calculate-price       ← 把最优方案按 productCode + roundList + couponCode 提交，逐行复核
```

### ① query-nearby-stores

**入参**：`city`、`keyword`、`searchType=2`（按位置搜）、`beType=1`（到店自取）
**用途**：用户不想查门店编码时，用「城市 + 地标」直接定位，拿 `storeCode`
**注意**：`beType=1` 到店自取不返回 `beCode`，后续所有调用都不传

### ② query-meals

**入参**：`storeCode`、`orderType=1`、`beType=1`
**取用字段**：
- `data.meals[code].currentPrice` → 单品现价（元，字符串，本项目转成「分」做整数运算）
- `data.meals[code].originalPrice` → 原价，用于「顺带多拿了多少东西」的价值计算
- `data.categories[].meals[].tags` → 区分单品与套餐

### ③ query-meal-detail

**这是整个项目的地基。** 没有它，套餐就是一个黑盒，无法知道里面有没有目标商品。

**入参**：`storeCode`、`orderType=1`、`beType=1`、`code`（套餐编码）
**取用字段**：
- `data.rounds[].choices[].code` → 轮次内可选的商品编码
- `data.rounds[].choices[].diffPrice` → 换成这个选项要加减多少钱（`"+ ¥0"` / `"- ¥1"`）
- `data.rounds[].minQuantity` / `maxQuantity` → 每个轮次必须选几件

**组合价算法**：`套餐价 = 菜单标价 + Σ(所选选项的 diffPrice)`
例如人气经典随心配标价 ¥14.90，蓝区选「麦香鸡」diffPrice 为 −¥1.00，
粉区选「小杯玉米杯」diffPrice 为 +¥0，实际就是 ¥13.90 —— 与官方计价接口返回一致。
### ③-b 必须处理的陷阱：随单购卡专属价

`query-meals` 返回的部分商品带这两个字段：

```json
{
  "9900010110": {
    "name": "鱼牛汉堡四件套随心选",
    "currentPrice": "32",
    "discountType": "随单购麦金卡优惠",
    "canWithOrder": true,
    "withOrder": { "membershipCode": "106", "specId": "880" }
  }
}
```

**`currentPrice` 32.00 是「同时买麦金卡」才成立的价格。** 实测对比：

| 提交方式 | `calculate-price` 返回 |
|---|---|
| 只传 `productCode` + `quantity` | **¥59.50** |
| 传 `withOrder`（membershipCode 106 / specId 880） | ¥32.00 + 选项差价 |

如果不处理，工具会认为「32 元的四件套里有一个巨无霸」，得出比真实情况乐观得多的结论。
本项目的处理：

1. `Meal.requires_card` 判定：`discountType` 含「随单购」且 `withOrder` 非空
2. 默认从候选池里**整体排除**这类商品（该店 104 个 SKU 里有 17 个属于此类）
3. 用户显式加 `--include-card-items` 时才纳入，并在核价时自动补齐
   `withOrder: {membershipCode, membershipSpecId}`

### ④ query-store-coupons / query-my-coupons / available-coupons

**这是「券能不能算进去」的关键三件套。** 优先级是：
`query-store-coupons`（当前门店真实可用）→ `query-my-coupons`（卡包，未校验门店）
→ `available-coupons`（可领券，只能做上限预演）。

**入参**：`storeCode`、`orderType=1`、`beType=1`（`store-coupons` 还需要门店已确定）
**取用字段**：券名、`couponCode`、`couponId`，以及接口若直接给了 `productCode` 就以它为准。

三条实测结论（决定了整个券模块的设计）：

1. **券绑定在商品行上，不是订单级满减。**
   入口在 `calculate-price.items[].couponCode` / `couponId`，而不是某个 order 级字段。
   所以一张「11.9 元麦辣鸡腿堡」在求解器里就是一个 `contents=((麦辣鸡腿汉堡,1),)`、
   `price=1190` 的购买单元。
2. **假券码会被服务端直接拒绝。**
   传一个不存在的 `couponCode`，返回「未找到优惠券或已赠送给好友」，
   而不是静默按原价算。这让 `calculate-price` 天然成为「券用对了没有」的校验器。
3. **券名 ≠ 菜单名。**
   同一家店，券写「麦辣鸡腿堡」，菜单写「麦辣鸡腿汉堡」；券写「脆薯饼」，菜单写
   「心形薯饼5块」；券写「中杯冰美式」，菜单写「麦咖啡™美式」。
   所以商品匹配分三级：精确包含 → 最长公共子串 → 失败，并用 `confidence`
   字段把「精确 / 相似 / 失败」带回给用户（相似匹配在报告里打 `[预演?]`）。

**空券包的处理**：`query-store-coupons` 对没有券的账号返回 `[]`，这不算错误。
此时 `auto` 模式退回 `available-coupons` 做预演——**明确标注「未领到、无券码、仅供上限参考」**，
并且不让这些方案进入官方核价（否则会拿假券码去撞服务端的拒绝）。

### ⑤ 本地求解

不调用 MCP，纯本地计算（`maxout/solver.py`）：

1. 把每个候选单元打分：`(目标件数, 其他件数, 其他原价合计)`
2. Pareto 剪枝：更贵且各项都不更强的单元直接淘汰，9,212 → 百量级
3. **券单元单独成池**：每张券有 `uses` 次数上限（真券为 1），不能进无限背包；
   `uses>1` 时按二进制分解成 1+2+4+… 个 0/1 块，再「枚举 2^k 子集」
4. 动态规划：`dp[c] = 花不超过 c 分能达到的最优四元组`，字典序比较
5. 组合：`最优 = max over 券子集 S ( 券收益(S) + 无券 DP(预算 − S 的花费) )`
6. 回溯出每个方案买了几份什么，同一条券的多个块合并成「用 N 次」展示

预算 100 元 = 10,000 个状态、券块 ≤ 14 个 = 16,384 个子集，毫秒级出全局最优解，结果可复现。

### ⑥ calculate-price

**入参**：`storeCode`、`orderType=1`、`beType=1`、`items[]`
**关键**：套餐必须带 `roundList`，按轮次声明选了什么，否则官方按默认搭配算价

```json
{
  "productCode": "9900013304",
  "quantity": 6,
  "roundList": [
    {"round": "1", "comboItemList": [{"code": "1120", "quantity": 1}]},
    {"round": "2", "comboItemList": [{"code": "4437", "quantity": 1}]}
  ]
}
```

**带券的行**在同一个 item 上多两个字段，且**必须和原价行分开传**，
不能把数量合并（合并就等于放弃这张券）：

```json
{ "productCode": "1440", "quantity": 1, "couponCode": "<couponCode>", "couponId": "<couponId>" },
{ "productCode": "1440", "quantity": 2 }
```

**返回**：`data.price` 是实付总额，**单位是分**；`data.discount` 是订单级优惠；
`data.productList[]` 每行都有 `originalSubtotal`（原价）和 `subtotal`（实付）。
本项目不只比对总额，还会逐行比对——总额对上但某一行错了，说明券用错了位置。

## 业务价值

| 价值点 | 说明 |
|---|---|
| 把「猜」换成「算」 | 组合优化是 NP 问题，大模型心算会漏掉随心配这类三层价格结构；DP 给出可验证的全局最优 |
| 券真的被用上，且只用一次 | 券变成「一次性道具」后，问题仍是背包的变体；用「子集枚举 × 无限背包」保持全局最优，而不是贪心用券 |
| 真实可下单 | 每个候选单元都能直接映射成 `create-order` 的入参（`productCode` + `quantity` + `roundList` + `couponCode`），不是纸上方案 |
| 价格可复核 | 结论必须过 `calculate-price`，券行逐行比对；券码不对会被服务端直接拒绝 |
| 不假装有券 | 没领到的券只做上限预演并明确标注，不会把它当成能下单的方案 |
| 零依赖易审计 | 纯标准库，任何人 clone 下来读一遍就能确认没有偷数据、没有埋后门 |

## 调用量

一次完整的「100 元最多买几个」运行：

- `query-meals` × 1
- `query-meal-detail` × 44（每个套餐一次，可缓存复用）
- `query-store-coupons` × 1（为空时 `query-my-coupons` × 1，再退回 `available-coupons` × 1）
- `calculate-price` × 1（仅在 `--verify` 且方案里有真实券码时）

单次运行约 47 次只读调用，不产生任何订单、不扣任何款。

> 注：`auto-bind-coupons`（自动领券）属于写操作，本项目**不调用**。
> 实测该接口对服务端发起的调用会返回「系统检测您的账号环境存在异常，暂无法领券」，
> 这属于账号风控，与本工具无关；本项目因此只读券、不领券。
