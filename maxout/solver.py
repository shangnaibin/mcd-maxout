"""求解器：给定预算，让目标餐品的份数最大化。

这是个整数规划问题（预算内、购买单元可重复、目标是某一商品的件数最大），
用「精确动态规划」求解，不靠模型猜、不靠贪心近似：

    dp[c] = 花掉不超过 c 分时，能达到的最好结果

「最好」按字典序比较：
    1. 目标商品份数最多
    2. 顺带买到的其他餐品件数最多（同价别浪费）
    3. 顺带餐品的原价合计最大
    4. 花得更少（余钱更多）

加入优惠券后，问题多了一层约束：**一张券只能用一次**。
于是把它拆成两个都保证最优的部分：

    最优解 = max over 券子集 S ( 券的收益(S) + 无券无限背包(预算 − 券花费(S)) )

券的数量很少（手机卡包里能同时用在同一个目标上的券通常 < 10 张），
2^k 枚举完全在毫秒级；而「无券部分」是标准无限背包，DP 精确。
两部分组合起来仍是全局最优——因为无券单元之间可以任意替换，先后顺序无关。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .catalog import Bundle, fmt

MAX_ONESHOT = 18          # 参与子集枚举的券块上限，2^18 = 262144 组合


@dataclass
class Plan:
    items: list[tuple[Bundle, int]]
    target_count: int
    extras_count: int
    extras_value: int
    spend: int
    budget: int
    retail: int = 0                          # 全部内容按单点价合计
    coupon_savings: int = 0                  # 券带来的直接减免
    coupons_used: list[str] = field(default_factory=list)
    exact: bool = True                       # 券块被截断时为 False（结果可能次优）
    card_fee: int = 0                        # 开卡费（已计入 spend）
    card_name: str = ""                      # 走的是哪张卡
    card_uses: int = 0                       # 用了会员价的次数

    @property
    def goods_spend(self) -> int:
        """商品实付（不含卡费）。"""
        return self.spend - self.card_fee

    @property
    def member_units(self) -> list[tuple[Bundle, int]]:
        return [(b, q) for b, q in self.items if b.card]

    @property
    def leftover(self) -> int:
        return self.budget - self.spend

    @property
    def coupon_count(self) -> int:
        """这个方案实际消耗了几张券（含重复使用的次数）。"""
        return sum(bundle.uses * qty for bundle, qty in self.items if bundle.coupon)

    @property
    def orders(self) -> int:
        """按「每单只能用一张券」的保守规则，需要拆成几单。

        券其实是绑在商品行上的，同一单多行各带一张券在接口层面成立，
        所以这是「最坏情况」的拆单数，真实平台不一定需要拆。
        """
        return max(1, self.coupon_count)

    @property
    def total_saving(self) -> int:
        """相对「全部按单点原价买」总共省了多少（含套餐结构 + 券）。"""
        return max(self.retail - self.spend, 0)

    def codes(self) -> dict[str, int]:
        total: dict[str, int] = {}
        for bundle, qty in self.items:
            for code, per in bundle.contents:
                total[code] = total.get(code, 0) + per * qty
        return total


CONDIMENT_HINTS = ("酱", "蘸", "调料", "椒盐")


def _usefulness(name: str) -> float:
    """余钱的「有用程度」权重：能吃能喝的正常算，蘸酱调味包打折。

    只影响「同样份数下怎么花余钱」这个次级排序，不影响目标商品份数的最优性。
    """
    return 0.2 if any(word in name for word in CONDIMENT_HINTS) else 1.0


def _score(bundle: Bundle, target_code: str | None, meals) -> tuple[int, int, int]:
    """bundle 在优化目标上的贡献：(目标件数, 其他件数, 其他价值)。"""
    target = bundle.count_of(target_code) if target_code else 0
    extras = 0
    value = 0
    for code, qty in bundle.contents:
        if code == target_code:
            continue
        meal = meals.get(code)
        weight = _usefulness(meal.name) if meal else 0.5
        if weight >= 1.0:
            extras += qty
        if meal:
            value += int(meal.original * qty * weight)
    return target, extras, value


def _pareto(bundles: list[Bundle], budget: int, target_code: str | None, meals) -> list[tuple[Bundle, tuple[int, int, int]]]:
    """砍掉「更贵且收益不更高」的冗余单元，压缩搜索空间。"""
    scored = []
    for bundle in bundles:
        if bundle.price <= 0 or bundle.price > budget:
            continue
        scored.append((bundle, _score(bundle, target_code, meals)))

    # 价钱相同就只留综合收益最高的那个
    best_per_price: dict[int, tuple[Bundle, tuple[int, int, int]]] = {}
    for bundle, score in scored:
        current = best_per_price.get(bundle.price)
        if current is None or score > current[1]:
            best_per_price[bundle.price] = (bundle, score)

    # 再按价格从低到高扫一遍，收益被「更便宜且更强」的单元全面压住的直接丢掉
    result = []
    frontier: list[tuple[int, int, int]] = []
    for price in sorted(best_per_price):
        bundle, score = best_per_price[price]
        dominated = any(
            other[0] >= score[0] and other[1] >= score[1] and other[2] >= score[2]
            for other in frontier
        )
        if not dominated:
            result.append((bundle, score))
            frontier.append(score)
    return result


def _unlimited(normal_bundles: list[Bundle], budget: int, target_code: str | None, meals,
               member_bundles: list[Bundle] = (), quota: int = 0):
    """无张数限制的部分：标准无限背包（+ 会员价的可选额度）。

    返回一个 _DP，含 index/前缀最优数组，供回溯与券子集枚举使用。

    会员价单元之间共享一个额度 `quota`（官方规则是「每日共 2 次」），
    所以这里做分层 DP：第 k 层 = 已经用掉 k 次会员价额度。
    同层内用普通单元可以无限次松弛；用会员价单元则跳到下一层。
    层内无限、层间单向，所以按 k 递增、c 递增扫一遍就是精确解。
    """
    scored = _pareto(normal_bundles, budget, target_code, meals)
    mscored = _pareto(member_bundles, budget, target_code, meals) if quota > 0 else []
    K = quota if mscored else 0

    units = [b for b, _ in scored] + [b for b, _ in mscored]
    scores = [s for _, s in scored] + [s for _, s in mscored]
    nm = len(scored)

    size = budget + 1
    T = [[-1] * size for _ in range(K + 1)]
    E = [[0] * size for _ in range(K + 1)]
    V = [[0] * size for _ in range(K + 1)]
    PS = [[-1] * size for _ in range(K + 1)]     # 前驱状态（编码为 k*size+c）
    PU = [[-1] * size for _ in range(K + 1)]     # 前驱用的单元下标
    T[0][0] = 0

    for k in range(K + 1):
        tk, ek, vk = T[k], E[k], V[k]
        for cost in range(size):
            base_t = tk[cost]
            if base_t < 0:
                continue
            bt, be, bv = base_t, ek[cost], vk[cost]
            for i in range(nm):
                unit = units[i]
                nxt = cost + unit.price
                if nxt > budget:
                    continue
                st, se, sv = scores[i]
                ct, ce, cv = bt + st, be + se, bv + sv
                if tk[nxt] < 0 or (ct, ce, cv) > (tk[nxt], ek[nxt], vk[nxt]):
                    tk[nxt], ek[nxt], vk[nxt] = ct, ce, cv
                    PS[k][nxt] = k * size + cost
                    PU[k][nxt] = i
            if k == K:
                continue
            nk = k + 1
            for i in range(nm, len(units)):
                unit = units[i]
                nxt = cost + unit.price
                if nxt > budget:
                    continue
                st, se, sv = scores[i]
                ct, ce, cv = bt + st, be + se, bv + sv
                if T[nk][nxt] < 0 or (ct, ce, cv) > (T[nk][nxt], E[nk][nxt], V[nk][nxt]):
                    T[nk][nxt], E[nk][nxt], V[nk][nxt] = ct, ce, cv
                    PS[nk][nxt] = k * size + cost
                    PU[nk][nxt] = i

    # 转成「预算 c 以内」的前缀最优，同时记住它落在哪一层、花到哪
    best = [(0, 0, 0, 0, 0, 0)] * size   # (t, e, v, -cost, k, cost)
    running = (0, 0, 0, 0, 0, 0)
    for cost in range(size):
        for k in range(K + 1):
            if T[k][cost] < 0:
                continue
            cand = (T[k][cost], E[k][cost], V[k][cost], -cost, k, cost)
            if cand > running:
                running = cand
        best[cost] = running
    return _DP(units, scores, nm, K, size, T, PS, PU, best, scored)


class _DP:
    """分层 DP 的结果容器：既能回溯方案，也能给券子集枚举报预算下标。"""

    def __init__(self, units, scores, nm, K, size, T, PS, PU, best, scored):
        self.units = units
        self.scores = scores
        self.nm = nm
        self.K = K
        self.size = size
        self.T = T
        self.PS = PS
        self.PU = PU
        self.best = best
        self.scored = scored

    def value(self, budget: int) -> tuple[int, int, int, int]:
        """预算 budget 内的最优 (目标件数, 其他件数, 其他价值, -花费)。"""
        return self.best[budget][:4]

    def items(self, budget: int) -> list[tuple[Bundle, int]]:
        """回溯出预算 budget 内的具体购买清单。"""
        _t, _e, _v, _neg, k, cost = self.best[budget]
        counts: dict[int, int] = {}
        guard = 0
        while cost > 0 and guard < 100000:
            guard += 1
            unit_index = self.PU[k][cost]
            if unit_index < 0:
                break
            counts[unit_index] = counts.get(unit_index, 0) + 1
            prev = self.PS[k][cost]
            if prev < 0:
                break
            k, cost = divmod(prev, self.size)
        rows = [(self.units[i], qty) for i, qty in counts.items()]
        rows.sort(key=lambda pair: -pair[0].price)
        return rows


def solve(bundles: list[Bundle], budget: int, target_code: str | None, meals,
          banned_skus: set[str] | None = None, card: str | None = None,
          quota: int = 0, card_fee: int = 0, card_name: str = "") -> Plan | None:
    banned_skus = banned_skus or set()
    normal: list[Bundle] = []
    member: list[Bundle] = []
    shots: list[Bundle] = []
    for bundle in bundles:
        if bundle.sku in banned_skus:
            continue
        if bundle.card:
            # 会员价单元只在「已开卡」的分支里存在，且消耗卡的次数额度
            if card and bundle.card == card and quota > 0:
                member.append(bundle)
            continue
        if bundle.one_shot:
            shots.append(bundle)
        else:
            normal.append(bundle)

    # 只对「真实券」按券码去重（防止同一张券从门店券和卡包各返回一次）。
    # 规则券不能去重：二进制分解会合法地产生两个内容相同的块
    # （例如 4 次 = 1+2+1），去掉任一块都会把可用次数算少。
    exact_codes: set[str] = set()
    kept: list[Bundle] = []
    for bundle in shots:
        if bundle.price <= 0 and not (bundle.coupon and bundle.coupon.kind == "free"):
            continue
        if bundle.price > budget:
            continue
        coupon = bundle.coupon
        if coupon is not None and coupon.usable:
            key = coupon.code or coupon.coupon_id
            if key in exact_codes:
                continue
            exact_codes.add(key)
        kept.append(bundle)
    shots = kept

    exact = True
    if len(shots) > MAX_ONESHOT:
        # 块数太多时截断，按「单位花费带来的券收益」优先保留。
        # 截断只会让可用次数变少，不会突破约束，但结果可能不是全局最优。
        shots.sort(
            key=lambda b: (b.coupon.saving * b.uses if b.coupon else 0) / max(b.price, 1),
            reverse=True,
        )
        shots = shots[:MAX_ONESHOT]
        exact = False

    dp = _unlimited(normal, budget, target_code, meals, member, quota)
    shot_scores = [_score(b, target_code, meals) for b in shots]

    best_total = None
    best_pick: list[int] = []
    best_rest = 0

    def walk(index: int, cost: int, picked: list[int], value: tuple[int, int, int, int]) -> None:
        nonlocal best_total, best_pick, best_rest
        if index == len(shots):
            if cost > budget:
                return
            rest = dp.value(budget - cost)
            combined = (
                value[0] + rest[0],
                value[1] + rest[1],
                value[2] + rest[2],
                value[3] + rest[3],
            )
            if best_total is None or combined > best_total:
                best_total = combined
                best_pick = list(picked)
                best_rest = cost
            return

        walk(index + 1, cost, picked, value)

        bundle = shots[index]
        if cost + bundle.price <= budget:
            target, extras, worth = shot_scores[index]
            picked.append(index)
            walk(
                index + 1,
                cost + bundle.price,
                picked,
                (value[0] + target, value[1] + extras, value[2] + worth, value[3] - bundle.price),
            )
            picked.pop()

    walk(0, 0, [], (0, 0, 0, 0))
    if best_total is None:
        return None

    items: list[tuple[Bundle, int]] = [(shots[i], 1) for i in best_pick]
    items.extend(dp.items(budget - best_rest))
    # 只为好读：贵的排前面，券行不会淹没在末尾
    items.sort(key=lambda pair: -pair[0].price)

    spend = sum(bundle.price * qty for bundle, qty in items) + card_fee
    extras_count = sum(_score(b, target_code, meals)[1] * q for b, q in items)
    extras_value = sum(_score(b, target_code, meals)[2] * q for b, q in items)
    target_count = sum(_score(b, target_code, meals)[0] * q for b, q in items)

    retail = 0
    for code, qty in _codes_of(items).items():
        meal = meals.get(code)
        if meal:
            retail += meal.original * qty
    coupon_savings = sum(
        bundle.coupon.saving * bundle.uses * qty for bundle, qty in items if bundle.coupon
    )
    # 同一条券可能被拆成多个块（×1 / ×2 / ×4 …），展示时按券名合并张数
    coupon_roll: dict[str, tuple[int, int]] = {}
    for bundle, qty in items:
        coupon = bundle.coupon
        if not coupon:
            continue
        times = bundle.uses * qty
        total, per = coupon_roll.get(coupon.name, (0, coupon.saving))
        coupon_roll[coupon.name] = (total + times, per)
    coupons_used = [
        f"{name}（{times} 张，共省 {fmt(per * times)}）"
        for name, (times, per) in coupon_roll.items()
    ]

    return Plan(
        items=items,
        target_count=target_count,
        extras_count=extras_count,
        extras_value=extras_value,
        spend=spend,
        budget=budget,
        retail=retail,
        coupon_savings=coupon_savings,
        coupons_used=coupons_used,
        exact=exact,
        card_fee=card_fee,
        card_name=card_name,
        card_uses=sum(qty for b, qty in items if b.card),
    )


def _codes_of(items: list[tuple[Bundle, int]]) -> dict[str, int]:
    total: dict[str, int] = {}
    for bundle, qty in items:
        for code, per in bundle.contents:
            total[code] = total.get(code, 0) + per * qty
    return total


def solve_without_coupons(bundles: list[Bundle], budget: int, target_code: str | None, meals) -> Plan | None:
    """对照组：忽略全部优惠券，只看菜单结构与套餐。

    注意要按 `coupon is None` 过滤，而不是按 `one_shot`：
    「不限次」的券单元是 one_shot=False，但它依然是券，不能混进无券方案。
    """
    return solve([b for b in bundles if b.coupon is None], budget, target_code, meals)


def solve_card_choice(bundles: list[Bundle], budget: int, target_code: str | None, meals,
                      card) -> tuple[Plan | None, Plan | None]:
    """开卡 / 不开卡两条分支各求一次，返回 (开卡方案, 不开卡方案)。

    卡费从预算里扣，所以开卡分支的价格上限是 budget - fee；
    但报告里的 spend 含卡费，这样「实付」才和真实账单一致。
    """
    plain = solve(bundles, budget, target_code, meals)
    if card.fee >= budget or card.uses <= 0:
        return None, plain
    member = solve(
        bundles,
        budget - card.fee,
        target_code,
        meals,
        card=card.key,
        quota=card.uses,
        card_fee=card.fee,
        card_name=card.name,
    )
    if member is not None:
        member.budget = budget        # 预算仍是用户给的那个数，卡费已含在实付里
    return member, plain


def pick_better(a: Plan | None, b: Plan | None) -> Plan | None:
    """按求解器同一套字典序比较两个方案。"""
    if a is None:
        return b
    if b is None:
        return a
    key_a = (a.target_count, a.extras_count, a.extras_value, a.leftover)
    key_b = (b.target_count, b.extras_count, b.extras_value, b.leftover)
    return a if key_a >= key_b else b


def alternatives(bundles: list[Bundle], budget: int, target_code: str | None, meals,
                 base: Plan, limit: int = 3, **solve_kwargs) -> list[Plan]:
    """换掉主力玩法再解一次，给出「同样份数的其他买法」。"""
    plans: list[Plan] = []
    tried: set[str] = set()
    for bundle, _qty in base.items:
        if bundle.sku in tried:
            continue
        tried.add(bundle.sku)
        alt = solve(bundles, budget, target_code, meals, banned_skus={bundle.sku}, **solve_kwargs)
        if alt and alt.target_count == base.target_count and (alt.spend, -alt.extras_value) != (base.spend, -base.extras_value):
            plans.append(alt)
        if len(plans) >= limit:
            break
    return plans


def render_plan(plan: Plan, meals, target_code: str | None, title: str = "最优方案") -> str:
    target_name = meals[target_code].name if target_code and target_code in meals else "目标商品"
    lines = [f"{title}：最多 {plan.target_count} 个{target_name}"]

    # 同一条券在求解时可能被拆成 1/2/4… 多个块，展示时要合回「用了几次」
    coupon_rows: dict[int, list] = {}
    plain_rows: list[tuple[Bundle, int]] = []
    for bundle, qty in plan.items:
        coupon = bundle.coupon
        if coupon is None:
            plain_rows.append((bundle, qty))
            continue
        row = coupon_rows.get(id(coupon))
        if row is None:
            coupon_rows[id(coupon)] = [coupon, bundle.uses * qty]
        else:
            row[1] += bundle.uses * qty

    for coupon, times in coupon_rows.values():
        meal = meals.get(coupon.target_code)
        package = f"{meal.name}×{coupon.quantity}" if meal else coupon.target_code
        lines.append(
            f" 券 用 {times} 次  {coupon.name}（{package}）"
            f"  @{fmt(coupon.price)}/次  小计 {fmt(coupon.price * times)}"
        )
    for bundle, qty in plain_rows:
        lines.append(f"   ×{qty:<2} {bundle.label}  @{fmt(bundle.price)}  小计 {fmt(bundle.price * qty)}")
    if plan.card_fee:
        lines.append(f"   +  开{plan.card_name} {fmt(plan.card_fee)}（会员价用 {plan.card_uses} 次）")
    lines.append(
        f"  实付 {fmt(plan.spend)} / 预算 {fmt(plan.budget)}，余额 {fmt(plan.leftover)}"
    )
    if plan.coupon_savings:
        lines.append(f"  其中优惠券抵扣 {fmt(plan.coupon_savings)}（{plan.coupon_count} 张券）")
        if plan.coupon_count > 1:
            lines.append(f"  按「每单只能用 1 张券」算，需要拆成 {plan.orders} 单下单")
    if plan.extras_count:
        lines.append(f"  顺带还有 {plan.extras_count} 件其他餐品（原价合计 {fmt(plan.extras_value)}）")
    if plan.retail > plan.spend:
        lines.append(f"  按单点原价要 {fmt(plan.retail)}，这一单总共省 {fmt(plan.total_saving)}")
    return "\n".join(lines)
