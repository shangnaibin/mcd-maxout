"""离线自测：证明「券子集 × 无限背包」和暴力枚举得到的是同一个最优解。

不联网、不依赖 MCP。跑法：

    python -m tests.test_optimality

为什么值得写这个测试：券是「一次性道具」，一旦为了省事改用贪心（先把手上的券
按面额从大到小用掉），就会在「这张券省得多、但占掉的预算会少买一件目标商品」
这种局面上算错。这个测试就是拿暴力枚举来钉死这一点。
"""
from __future__ import annotations

import itertools
import random
import sys

from maxout.catalog import Bundle, Meal
from maxout.coupons import Coupon
from maxout.solver import solve


def _meal(code: str, name: str, price: int) -> Meal:
    return Meal(code=code, name=name, price=price, original=price)


def _brute_force(bundles: list[Bundle], budget: int, target: str, meals) -> tuple[int, int]:
    """枚举所有组合（预算小的时候可行），返回 (最多目标件数, 达成时的最低花费)。"""
    best = (0, 0)
    normal = [b for b in bundles if not b.one_shot]
    shots = [b for b in bundles if b.one_shot]

    # 券的每种用/不用组合
    for mask in range(1 << len(shots)):
        cost = 0
        target_count = 0
        for index, bundle in enumerate(shots):
            if mask >> index & 1:
                cost += bundle.price
                target_count += bundle.count_of(target)
        if cost > budget:
            continue
        # 剩下的预算用无券单元凑，逐层递归
        stack = [(cost, target_count)]
        while stack:
            spent, count = stack.pop()
            if (count, -spent) > (best[0], -best[1]):
                best = (count, spent)
            for bundle in normal:
                if spent + bundle.price <= budget:
                    stack.append((spent + bundle.price, count + bundle.count_of(target)))
    return best


def _brute_force_multi(bundles: list[Bundle], budget: int, target: str, meals,
                       coupons: list[Coupon]) -> int:
    """券可以重复使用时，枚举每张券实际用了几次，再拿无券单元填满预算。"""
    best = 0
    normal = [b for b in bundles if b.coupon is None]
    ranges = [range(0, coupon.uses + 1) for coupon in coupons]
    for amounts in itertools.product(*ranges):
        cost = 0
        count = 0
        for coupon, times in zip(coupons, amounts):
            cost += coupon.price * times
            if coupon.target_code == target:
                count += coupon.quantity * times
        if cost > budget:
            continue
        stack = [(cost, count)]
        while stack:
            spent, got = stack.pop()
            if got > best:
                best = got
            for bundle in normal:
                nxt = spent + bundle.price
                if nxt <= budget:
                    stack.append((nxt, got + bundle.count_of(target)))
    return best


def _multi_use_cases() -> int:
    """专项：券可重复使用（二进制分解）时，解是否仍是全局最优。"""
    from maxout.coupons import coupon_bundles

    random.seed(7)
    failures = 0
    for case in range(30):
        meals = {
            "A": _meal("A", "目标汉堡", 2300),
            "B": _meal("B", "配餐饮料", 800),
        }
        base = [
            Bundle(kind="single", sku="A", label="目标汉堡", price=2300, contents=(("A", 1),)),
            Bundle(kind="single", sku="B", label="配餐饮料", price=800, contents=(("B", 1),)),
            Bundle(kind="combo", sku="M1", label="套餐", price=1490, contents=(("A", 1), ("B", 1))),
        ]
        coupons: list[Coupon] = []
        for index in range(random.randint(1, 3)):
            kind = random.choice(["A", "B"])
            qty = random.choice([1, 2])
            coupons.append(
                Coupon(
                    name=f"多次券{index}", kind="fixed", target_code=kind, quantity=qty,
                    price=random.randint(300, 1600),
                    retail=meals[kind].price * qty,
                    uses=random.randint(1, 4),
                )
            )
        coupon_units, _ = coupon_bundles(coupons, meals)
        bundles = base + coupon_units
        budget = random.choice([3000, 5000])

        plan = solve(bundles, budget, "A", meals)
        expected = _brute_force_multi(bundles, budget, "A", meals, coupons)
        got = plan.target_count if plan else 0
        if got != expected:
            print(f"[FAIL] 多次券 case {case}: {got} != 暴力枚举 {expected}")
            failures += 1
            continue
        # 券用量必须真的没超过上限
        if plan:
            for coupon in coupons:
                used = sum(b.uses * q for b, q in plan.items if b.coupon is coupon)
                if used > coupon.uses:
                    print(f"[FAIL] 多次券 case {case}: {coupon.name} 用了 {used} 次 > 上限 {coupon.uses}")
                    failures += 1
    return failures


def _card_cases() -> int:
    """专项：开卡分支 + 会员价次数额度，是否仍是全局最优。

    这一层的坑在于「会员价不是无限用」：官方规则是每天共 2 次。
    如果实现成无限次，结果会明显虚高；如果实现成只用 1 次，又会低估。
    这里把额度当约束，用暴力枚举（会员单元用了几次 × 普通单元填满）对答案。
    """
    random.seed(11)
    failures = 0
    for case in range(30):
        meals = {
            "A": _meal("A", "目标汉堡", 2300),
            "B": _meal("B", "配餐饮料", 800),
        }
        key = "106:880"
        normal = [
            Bundle(kind="single", sku="A", label="目标汉堡", price=2300, contents=(("A", 1),)),
            Bundle(kind="single", sku="B", label="配餐饮料", price=800, contents=(("B", 1),)),
        ]
        member = []
        for index in range(random.randint(1, 3)):
            kind = random.choice(["A", "B"])
            qty = random.choice([1, 1, 2])
            member.append(
                Bundle(
                    kind="single", sku=kind, label=f"会员{index}",
                    price=random.randint(400, 1800),
                    contents=((kind, qty),), card=key,
                )
            )
        quota = random.choice([1, 2, 3])
        fee = random.choice([0, 500, 1900])
        budget = random.choice([2500, 4000, 6000])

        plan = solve(normal + member, budget - fee, "A", meals,
                     card=key, quota=quota, card_fee=fee)
        expected = _brute_force_card(normal, member, budget - fee, "A", quota)
        got = plan.target_count if plan else 0
        if got != expected:
            print(f"[FAIL] 会员卡 case {case}: {got} != 暴力枚举 {expected}（额度 {quota}，卡费 {fee}）")
            failures += 1
            continue
        if plan:
            used = sum(q for b, q in plan.items if b.card)
            if used > quota:
                print(f"[FAIL] 会员卡 case {case}: 会员价用了 {used} 次 > 额度 {quota}")
                failures += 1
            if plan.spend > budget:
                print(f"[FAIL] 会员卡 case {case}: 含卡费超预算 {plan.spend} > {budget}")
                failures += 1
    return failures


def _brute_force_card(normal, member, budget: int, target: str, quota: int) -> int:
    """暴力：枚举会员价用了几次、用在哪几个单元，剩下的预算用普通单元递归填满。"""
    if budget < 0:
        return 0
    best = 0
    for k in range(quota + 1):
        for picked in itertools.combinations_with_replacement(range(len(member)), k):
            cost = sum(member[i].price for i in picked)
            if cost > budget:
                continue
            count = sum(member[i].count_of(target) for i in picked)
            stack = [(cost, count)]
            while stack:
                spent, got = stack.pop()
                if got > best:
                    best = got
                for bundle in normal:
                    nxt = spent + bundle.price
                    if nxt <= budget:
                        stack.append((nxt, got + bundle.count_of(target)))
    return best


def run() -> int:
    random.seed(20261020)
    failures = 0
    for case in range(40):
        meals = {
            "A": _meal("A", "目标汉堡", 2300),
            "B": _meal("B", "配餐饮料", 800),
            "C": _meal("C", "小食", 1200),
        }
        # 目标商品：单点 23 元，另有 14.9 的套餐（含 1 个目标 + 1 个饮料）
        bundles = [
            Bundle(kind="single", sku="A", label="目标汉堡", price=2300, contents=(("A", 1),)),
            Bundle(kind="single", sku="B", label="配餐饮料", price=800, contents=(("B", 1),)),
            Bundle(kind="single", sku="C", label="小食", price=1200, contents=(("C", 1),)),
            Bundle(kind="combo", sku="M1", label="套餐", price=1490, contents=(("A", 1), ("B", 1))),
        ]
        # 随机生成 0~3 张一次性券
        for index in range(random.randint(0, 3)):
            kind = random.choice(["A", "B", "C"])
            qty = random.choice([1, 1, 2])
            price = random.randint(300, 1500)
            coupon = Coupon(
                name=f"随机券{index}", kind="fixed", target_code=kind, quantity=qty,
                price=price, retail=meals[kind].price * qty, code=f"C{index}",
            )
            bundles.append(
                Bundle(
                    kind="coupon", sku=kind, label=coupon.name, price=price,
                    contents=((kind, qty),), coupon=coupon, one_shot=True,
                )
            )

        budget = random.choice([2000, 3000, 5000])
        plan = solve(bundles, budget, "A", meals)
        expected_count, _expected_spend = _brute_force(bundles, budget, "A", meals)
        got_count = plan.target_count if plan else 0
        if got_count != expected_count:
            print(f"[FAIL] case {case}: 最优件数 {got_count} != 暴力枚举 {expected_count}")
            failures += 1
        elif plan and plan.spend > budget:
            print(f"[FAIL] case {case}: 超预算 {plan.spend} > {budget}")
            failures += 1
    failures += _multi_use_cases()
    failures += _card_cases()
    return failures


if __name__ == "__main__":
    failed = run()
    if failed:
        print(f"{failed} 个用例失败")
        sys.exit(1)
    print("全部通过：含「券可重复使用」在内，解与暴力枚举一致")
