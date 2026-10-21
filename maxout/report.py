"""生成可分享的单文件 HTML 报告。

为什么要做这件事：这类工具的传播不靠功能，靠「有人愿意截图发出去」。
所以这一页只服务三个目的——

1. 三秒内看懂结论：一个大数字 + 一句人话
2. 可信：官方核价徽章、券的来源、每一行单价都摆出来
3. 好看：浅色卡片、麦当劳红黄、没有一行需要联网加载

单文件、内联样式、零外部依赖，双击就能打开，微信里发过去也不会散版。
"""
from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path

from .catalog import fmt
from .solver import Plan


def _e(text) -> str:
    return html.escape(str(text or ""), quote=False)


def _pct(value: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return max(min(value / total * 100, 100), 3)


def _kind_tag(bundle) -> str:
    if bundle.coupon:
        return '<span class="tag tag-coupon">券</span>'
    if bundle.kind == "combo":
        return '<span class="tag tag-combo">套餐</span>'
    return '<span class="tag tag-single">单品</span>'


def _rows(plan: Plan, meals) -> str:
    parts = []
    for bundle, qty in plan.items:
        unit = bundle.price
        parts.append(
            "<tr>"
            f'<td class="cell-name">{_kind_tag(bundle)}<span>{_e(bundle.label)}</span></td>'
            f'<td class="num">×{qty}</td>'
            f'<td class="num">{fmt(unit)}</td>'
            f'<td class="num strong">{fmt(unit * qty)}</td>'
            "</tr>"
        )
    if plan.card_fee:
        parts.append(
            "<tr>"
            '<td class="cell-name"><span class="tag tag-single">卡</span>'
            f'<span>开{_e(plan.card_name or "会员卡")}（会员价用 {plan.card_uses} 次）</span></td>'
            '<td class="num">×1</td>'
            f'<td class="num">{fmt(plan.card_fee)}</td>'
            f'<td class="num strong">{fmt(plan.card_fee)}</td>'
            "</tr>"
        )
    return "\n".join(parts)


def _coupon_cards(coupons, meals) -> str:
    if not coupons:
        return ""
    cards = []
    for coupon in coupons:
        meal = meals.get(coupon.target_code)
        target = meal.name if meal else coupon.target_code
        if coupon.usable:
            mark, cls = "真实券码", "real"
        elif coupon.confidence == "fuzzy":
            mark, cls = "预演·待确认", "fuzzy"
        else:
            mark, cls = "预演", "preview"
        cards.append(
            f'<div class="coupon {cls}">'
            f'<div class="coupon-name">{_e(coupon.name)}</div>'
            f'<div class="coupon-target">{_e(target)} ×{coupon.quantity}</div>'
            f'<div class="coupon-price"><s>{fmt(coupon.retail)}</s> → <b>{fmt(coupon.price)}</b></div>'
            f'<div class="coupon-mark">{mark} · 省 {fmt(coupon.saving)}</div>'
            "</div>"
        )
    return f"""
  <section class="card">
    <h2>券池 <span class="hint">{len(coupons)} 张</span></h2>
    <div class="coupon-grid">{''.join(cards)}</div>
  </section>"""


def render(
    *,
    plan: Plan,
    meals,
    target_code: str,
    target_name: str,
    store_name: str,
    store_code: str,
    budget: int,
    coupons,
    pool_source: str,
    baseline: int,
    no_coupon: Plan | None,
    verification: dict | None,
    card=None,
    card_plan: Plan | None = None,
    plain_plan: Plan | None = None,
    card_member_skus: int = 0,
    generated_at: datetime | None = None,
) -> str:
    now = generated_at or datetime.now()
    baseline_spend = baseline * (meals[target_code].price if target_code in meals else 0)
    max_count = max(plan.target_count, baseline, no_coupon.target_count if no_coupon else 0, 1)

    gap = plan.target_count - baseline
    gap_text = f"比单买多 {gap} 个" if gap > 0 else "与单买持平"
    gap_cls = "up" if gap > 0 else "flat"

    if verification and verification.get("skipped"):
        badge = '<span class="badge warn">预演券 · 未做官方核价</span>'
    elif verification and verification.get("amount"):
        ok = verification.get("matches_local")
        badge = (
            f'<span class="badge ok">官方核价 {fmt(verification["amount"])} {"一致" if ok else "有差异"}</span>'
        )
    else:
        badge = ""

    per_unit = plan.spend / plan.target_count if plan.target_count else 0
    per_unit_base = meals[target_code].price if target_code in meals else 0

    structure_saving = max(plan.retail - plan.goods_spend - plan.coupon_savings, 0)
    structure_seg = (
        f'<div class="m-seg m-structure" style="width:{_pct(structure_saving, max(plan.retail, 1)):.1f}%">'
        f"省 {fmt(structure_saving)}</div>"
        if structure_saving
        else ""
    )
    coupon_seg = (
        f'<div class="m-seg m-coupon" style="width:{_pct(plan.coupon_savings, max(plan.retail, 1)):.1f}%">'
        f"券 {fmt(plan.coupon_savings)}</div>"
        if plan.coupon_savings
        else ""
    )

    compare_rows = [
        ("只单买", baseline, baseline_spend, "bar-base"),
        (
            "算套餐、不算券",
            no_coupon.target_count if no_coupon else 0,
            no_coupon.spend if no_coupon else 0,
            "bar-mid",
        ),
        ("算套餐 + 用券", plan.target_count, plan.spend, "bar-best"),
    ]
    bars = "\n".join(
        f'<div class="bar-row">'
        f'<div class="bar-label">{label}</div>'
        f'<div class="bar-track"><div class="bar {cls}" style="width:{_pct(count, max_count):.1f}%">'
        f'<span class="bar-count">{count} 个</span></div></div>'
        f'<div class="bar-spend">{fmt(spend)}</div>'
        f"</div>"
        for label, count, spend, cls in compare_rows
        if count or label != "算套餐、不算券"
    )

    coupon_used = ""
    if plan.coupons_used:
        items = "".join(f"<li>{_e(note)}</li>" for note in plan.coupons_used)
        coupon_used = f"""
  <section class="card">
    <h2>这一单用掉的券</h2>
    <ul class="coupon-used">{items}</ul>
  </section>"""

    card_used = ""
    if card:
        head = (
            f"<h2>开{card.name}值不值 <span class=\"hint\">卡费 {fmt(card.fee)} · "
            f"会员价限 {card.uses} 次 · 会员价商品 {card_member_skus} 个</span></h2>"
        )
        if card_plan is None:
            body = f'<p class="note">卡费 {fmt(card.fee)} 已超过预算，开了也不划算。</p>'
        elif card_plan.card_uses == 0:
            body = (
                '<p class="note">会员价商品里没有能多囤「'
                f'{_e(target_name)}」的划算组合（会员价多为指定汉堡的四件套），'
                "这张卡对囤它没帮助。</p>"
            )
        else:
            diff = card_plan.target_count - (plain_plan.target_count if plain_plan else 0)
            extra_gap = card_plan.extras_count - (plain_plan.extras_count if plain_plan else 0)
            if diff > 0:
                verdict = (
                    f"开卡多 {diff} 个{_e(target_name)}"
                    + (f"，还多 {extra_gap} 件其他餐品" if extra_gap > 0 else "")
                )
            elif diff == 0 and extra_gap > 0:
                verdict = (
                    f"份数一样，但多 {extra_gap} 件其他餐品，"
                    f"多花 {fmt(card_plan.spend - plain_plan.spend)}"
                )
            elif diff == 0 and extra_gap == 0:
                verdict = f"东西一样，纯多花卡费 {fmt(card.fee)}，不值"
            else:
                bits = []
                if diff < 0:
                    bits.append(f"少 {-diff} 个{_e(target_name)}")
                if extra_gap > 0:
                    bits.append(f"但多 {extra_gap} 件其他餐品")
                elif extra_gap < 0:
                    bits.append(f"少 {-extra_gap} 件其他餐品")
                verdict = "开卡" + "、".join(bits) + "，为「多囤」不划算"
            body = (
                '<div class="card-cmp">'
                f'<div><b>开卡</b> {card_plan.target_count} 个 · 实付 {fmt(card_plan.spend)}'
                f"（卡费 {fmt(card.fee)} + 商品 {fmt(card_plan.goods_spend)}）· 顺带 {card_plan.extras_count} 件</div>"
                f'<div><b>不开卡</b> {plain_plan.target_count} 个 · 实付 {fmt(plain_plan.spend)}'
                f" · 顺带 {plain_plan.extras_count} 件</div>"
                f'<div class="verdict">→ {verdict}</div>'
                "</div>"
            )
        card_used = f"""
  <section class="card">{head}{body}</section>"""

    money = f"""
  <section class="card">
    <h2>钱花在哪 <span class="hint">按单点原价对照</span></h2>
    <div class="money-bar">
      {structure_seg}{coupon_seg}
      <div class="m-seg m-pay" style="width:{_pct(plan.spend, max(plan.retail, 1)):.1f}%">
        实付 {fmt(plan.spend)}
      </div>
    </div>
    <div class="money-legend">
      {'<span><i class="dot m-structure"></i>套餐结构省 ' + fmt(structure_saving) + '</span>' if structure_saving else ''}
      {'<span><i class="dot m-coupon"></i>优惠券省 ' + fmt(plan.coupon_savings) + '</span>' if plan.coupon_savings else ''}
      <span><i class="dot m-pay"></i>实付 {fmt(plan.spend)}</span>
      <span class="money-total">原价合计 {fmt(plan.retail)}</span>
    </div>
  </section>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(target_name)} · {fmt(budget)} 最多买 {plan.target_count} 个 ｜ 麦尽其用 MaxOut</title>
<meta name="description" content="{fmt(budget)} 在麦当劳最多能买 {plan.target_count} 个{_e(target_name)}，实付 {fmt(plan.spend)}，方案经官方计价接口核对。">
<style>
  :root {{
    --red: #DA291C;
    --yellow: #FFC72C;
    --ink: #1B1B1F;
    --sub: #6B6B73;
    --line: #EAEAEE;
    --bg: #F6F6F8;
    --card: #FFFFFF;
  }}
  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: var(--bg); }}
  body {{
    font-family: "PingFang SC", "Microsoft YaHei", system-ui, -apple-system, "Segoe UI", sans-serif;
    color: var(--ink);
    -webkit-font-smoothing: antialiased;
    padding: 28px 16px 48px;
  }}
  .page {{ width: 760px; max-width: 100%; margin: 0 auto; }}
  header {{
    display: flex; align-items: center; justify-content: space-between;
    padding: 0 4px 14px;
  }}
  .brand {{ font-weight: 800; font-size: 15px; letter-spacing: .5px; }}
  .brand span {{ color: var(--sub); font-weight: 500; font-size: 12px; margin-left: 6px; letter-spacing: 0; }}
  .brand i {{
    display: inline-block; width: 22px; height: 22px; border-radius: 7px;
    background: var(--yellow); vertical-align: -5px; margin-right: 8px;
    box-shadow: inset 0 0 0 2px rgba(0,0,0,.06);
  }}
  .meta {{ font-size: 12px; color: var(--sub); text-align: right; line-height: 1.6; }}

  .hero {{
    background: var(--card); border-radius: 22px; padding: 30px 32px 26px;
    box-shadow: 0 1px 2px rgba(20,20,40,.05), 0 12px 32px -12px rgba(20,20,40,.12);
    position: relative; overflow: hidden;
  }}
  .hero::after {{
    content: ""; position: absolute; right: -60px; top: -60px; width: 220px; height: 220px;
    border-radius: 50%; background: radial-gradient(circle at 30% 30%, #FFE7A3, #FFC72C 62%, #F5B400);
    opacity: .22;
  }}
  .hero-kicker {{ font-size: 13px; color: var(--sub); letter-spacing: .5px; }}
  .hero-num {{
    font-size: 96px; line-height: .95; font-weight: 800; letter-spacing: -3px;
    font-variant-numeric: tabular-nums; margin: 10px 0 2px;
  }}
  .hero-num small {{ font-size: 30px; font-weight: 700; letter-spacing: 0; margin-left: 6px; }}
  .hero-sub {{ font-size: 16px; color: var(--ink); margin-bottom: 18px; }}
  .hero-sub b {{ border-bottom: 3px solid var(--yellow); padding-bottom: 1px; }}
  .badges {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .badge {{
    font-size: 12.5px; padding: 6px 11px; border-radius: 999px; font-weight: 600;
    background: #F1F1F4; color: var(--sub);
  }}
  .badge.up {{ background: #FFEFEF; color: var(--red); }}
  .badge.flat {{ background: #F1F1F4; color: var(--sub); }}
  .badge.ok {{ background: #E8F7EE; color: #127A44; }}
  .badge.warn {{ background: #FFF6E0; color: #97690A; }}

  .card {{
    background: var(--card); border-radius: 20px; padding: 22px 24px;
    margin-top: 14px; box-shadow: 0 1px 2px rgba(20,20,40,.05);
  }}
  .card h2 {{
    font-size: 14px; margin: 0 0 16px; letter-spacing: .3px;
    display: flex; align-items: baseline; gap: 8px;
  }}
  .hint {{ font-size: 12px; color: var(--sub); font-weight: 400; }}

  .bar-row {{ display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }}
  .bar-row:last-child {{ margin-bottom: 0; }}
  .bar-label {{ width: 118px; font-size: 13px; color: var(--sub); flex: none; }}
  .bar-track {{ flex: 1; background: #F3F3F6; border-radius: 10px; height: 34px; overflow: hidden; }}
  .bar {{
    height: 100%; border-radius: 10px; display: flex; align-items: center;
    padding-left: 12px; transition: none;
  }}
  .bar-base {{ background: #DDDDE3; }}
  .bar-mid {{ background: #B9B9C4; }}
  .bar-best {{ background: linear-gradient(90deg, var(--yellow), #F5A623); }}
  .bar-count {{ font-size: 13px; font-weight: 700; font-variant-numeric: tabular-nums; }}
  .bar-best .bar-count {{ color: #3A2600; }}
  .bar-spend {{ width: 74px; text-align: right; font-size: 13px; color: var(--sub); font-variant-numeric: tabular-nums; flex: none; }}

  table {{ width: 100%; border-collapse: collapse; }}
  th {{
    text-align: left; font-size: 11.5px; color: var(--sub); font-weight: 500;
    padding: 0 0 10px; border-bottom: 1px solid var(--line);
  }}
  th.num, td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  td {{ padding: 12px 0; border-bottom: 1px solid #F4F4F7; font-size: 13.5px; }}
  tr:last-child td {{ border-bottom: none; }}
  .cell-name {{ display: flex; align-items: center; gap: 8px; padding-right: 12px; }}
  .strong {{ font-weight: 700; }}
  .tag {{
    font-size: 10.5px; padding: 2px 7px; border-radius: 6px; flex: none; font-weight: 600;
  }}
  .tag-coupon {{ background: #FFEFEF; color: var(--red); }}
  .tag-combo {{ background: #FFF6E0; color: #97690A; }}
  .tag-single {{ background: #F1F1F4; color: var(--sub); }}
  .total-row td {{ padding-top: 14px; font-size: 15px; border-top: 2px solid var(--ink); border-bottom: none; }}

  .money-bar {{ display: flex; height: 40px; border-radius: 10px; overflow: hidden; background: #F3F3F6; }}
  .m-seg {{
    display: flex; align-items: center; justify-content: center;
    font-size: 12px; font-weight: 700; color: #fff; white-space: nowrap; overflow: hidden;
  }}
  .m-structure {{ background: #F5A623; color: #3A2600; }}
  .m-coupon {{ background: var(--red); }}
  .m-pay {{ background: #33333A; }}
  .money-legend {{
    display: flex; flex-wrap: wrap; gap: 16px; margin-top: 14px;
    font-size: 12.5px; color: var(--sub); align-items: center;
  }}
  .dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 3px; margin-right: 6px; }}
  .money-total {{ margin-left: auto; color: var(--ink); font-weight: 600; }}

  .coupon-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); gap: 10px; }}
  .coupon {{ border: 1px solid var(--line); border-radius: 14px; padding: 13px 14px; }}
  .coupon.real {{ border-color: #CFE9D8; background: #F7FCF9; }}
  .coupon.fuzzy {{ border-style: dashed; }}
  .coupon-name {{ font-size: 13px; font-weight: 700; margin-bottom: 5px; }}
  .coupon-target {{ font-size: 11.5px; color: var(--sub); margin-bottom: 8px; }}
  .coupon-price {{ font-size: 13px; font-variant-numeric: tabular-nums; }}
  .coupon-price s {{ color: #A9A9B2; }}
  .coupon-price b {{ color: var(--red); font-size: 15px; }}
  .coupon-mark {{ font-size: 11px; color: var(--sub); margin-top: 7px; }}

  .coupon-used {{ margin: 0; padding-left: 18px; font-size: 13px; color: var(--ink); }}
  .coupon-used li {{ margin-bottom: 6px; }}
  .card-cmp div {{ font-size: 13px; color: var(--ink); padding: 6px 0; border-bottom: 1px dashed var(--line); }}
  .card-cmp .verdict {{ border-bottom: 0; color: var(--ink); font-weight: 600; }}
  .note {{ margin: 0; font-size: 13px; color: var(--sub); }}

  .per-unit {{ display: flex; gap: 28px; }}
  .pu {{ flex: 1; }}
  .pu-label {{ font-size: 12px; color: var(--sub); margin-bottom: 6px; }}
  .pu-value {{ font-size: 26px; font-weight: 800; font-variant-numeric: tabular-nums; letter-spacing: -.5px; }}
  .pu-value.win {{ color: var(--red); }}
  .pu-note {{ font-size: 11.5px; color: var(--sub); margin-top: 4px; }}

  footer {{
    margin-top: 18px; padding: 0 6px; font-size: 11.5px; color: var(--sub);
    line-height: 1.8; text-align: center;
  }}
  footer b {{ color: var(--ink); }}
  @media (max-width: 620px) {{
    .hero-num {{ font-size: 72px; }}
    .bar-label {{ width: 84px; font-size: 12px; }}
    .bar-spend {{ width: 62px; }}
    .per-unit {{ flex-direction: column; gap: 16px; }}
  }}
  @media print {{
    body {{ background: #fff; padding: 0; }}
    .card, .hero {{ box-shadow: none; border: 1px solid var(--line); }}
  }}
</style>
</head>
<body>
<div class="page">
  <header>
    <div class="brand"><i></i>麦尽其用<span>MaxOut</span></div>
    <div class="meta">{_e(store_name)}<br>{now.strftime("%Y-%m-%d %H:%M")} · 麦当劳 MCP 实时数据</div>
  </header>

  <section class="hero">
    <div class="hero-kicker">预算 {fmt(budget)} · 只买一种 · 份数最大化</div>
    <div class="hero-num">{plan.target_count}<small>个</small></div>
    <div class="hero-sub">最多能买到 <b>{_e(target_name)}</b>，实付 {fmt(plan.spend)}，还剩 {fmt(plan.leftover)}</div>
    <div class="badges">
      <span class="badge {gap_cls}">{gap_text}</span>
      <span class="badge">单份成本 {fmt(int(round(per_unit)))}</span>
      {badge}
    </div>
  </section>

  <section class="card">
    <h2>三种买法的差距</h2>
    {bars}
  </section>

  <section class="card">
    <h2>组合明细 <span class="hint">官方计价接口可复核</span></h2>
    <table>
      <thead>
        <tr><th>购买单元</th><th class="num">数量</th><th class="num">单价</th><th class="num">小计</th></tr>
      </thead>
      <tbody>
        {_rows(plan, meals)}
        <tr class="total-row">
          <td>合计</td><td class="num">—</td><td class="num">—</td>
          <td class="num strong">{fmt(plan.spend)}</td>
        </tr>
      </tbody>
    </table>
  </section>

  <section class="card">
    <h2>每份成本</h2>
    <div class="per-unit">
      <div class="pu">
        <div class="pu-label">单买 {_e(target_name)}</div>
        <div class="pu-value">{fmt(per_unit_base)}</div>
        <div class="pu-note">菜单标价</div>
      </div>
      <div class="pu">
        <div class="pu-label">这一单的实际成本</div>
        <div class="pu-value win">{fmt(int(round(per_unit)))}</div>
        <div class="pu-note">{('降了 ' + f'{100 - per_unit / per_unit_base * 100:.0f}%') if per_unit_base else ''}</div>
      </div>
    </div>
  </section>

{money}

{card_used}

{_coupon_cards(coupons, meals)}

{coupon_used}

  <footer>
    价格来自麦当劳中国 MCP：菜单 <b>query-meals</b>、套餐结构 <b>query-meal-detail</b>、
    券 <b>query-store-coupons</b>、核价 <b>calculate-price</b>。<br>
    {_e(pool_source)} · 门店编码 {_e(store_code)} · <b>非麦当劳官方产品</b>，价格与供应以门店实时结果为准。
  </footer>
</div>
</body>
</html>
"""


def write(path: str | Path, **kwargs) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(**kwargs), encoding="utf-8")
    return target
