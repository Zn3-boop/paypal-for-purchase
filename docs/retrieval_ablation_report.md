# 检索/重排策略消融报告（2026-11-14）

> 生成方式：`python scripts/retrieval_ablation.py --offline --cache docs/retrieval_live.json`
> 数据来源：20 条真实需求（与 quality_gate 同一批评测 query）× 真网 Channel3 返回
> （快照 `docs/retrieval_live.json`），判定全部来自生产函数 `satisfaction.evaluate`
> —— **零人工标注**。可随时用 `--update` 重刷缓存后复算。

## 结论（一句话版）

在同一批真实返回上，MMR 重排（λ≤0.7）把 top-3 的品类多样性均值从 **1.5 提升到 1.75（+16.7%）**，
代价是可判定率从 0.8682 微降到 0.8615（-0.8%，选入更多异质商品、部分缺证据）。
**λ=1.0 时 MMR 退化为纯相关排序**（多样性回到 1.5）——λ 参数真实起作用；
λ∈{0.3, 0.5, 0.7} 曲线重合说明该候选池规模下 λ 不敏感（曲线重合本身就是结论）。

诚实边界：个别 query 上 MMR 多样性反降（如团4 保温杯 3→2）——MMR 对候选池
本身已多样化的场景增益有限甚至为负；Hit/MRR 各策略全为 1.0，说明本次快照里
检索召回质量本身已足够「前 k 一定命中」，重排的战场在多样性而非召回率。

## 明细

====================================================================================================

[服1 半身裙] 候选 20 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0      1.0      1  0.4028
greedy       True    1.0      1.0      1  0.4028
mmr_l0.3     True    1.0      1.0      3  0.1948
mmr_l0.5     True    1.0      1.0      3  0.1948
mmr_l0.7     True    1.0      1.0      3  0.1948
mmr_l1.0     True    1.0      1.0      1  0.4028

[服2 针织开衫] 候选 20 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0      1.0      1  0.6604
greedy       True    1.0      1.0      1  0.6604
mmr_l0.3     True    1.0      1.0      2  0.6152
mmr_l0.5     True    1.0      1.0      2  0.6152
mmr_l0.7     True    1.0      1.0      2  0.6152
mmr_l1.0     True    1.0      1.0      1  0.6604

[服3 牛仔裤] 候选 20 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0      0.8      1  0.4087
greedy       True    1.0      0.8      1  0.4087
mmr_l0.3     True    1.0      0.8      1   0.916
mmr_l0.5     True    1.0      0.8      1   0.916
mmr_l0.7     True    1.0      0.8      1   0.916
mmr_l1.0     True    1.0      0.8      1  0.4087

[服4 皮鞋] 候选 20 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0     0.75      2  0.5289
greedy       True    1.0     0.75      2  0.5289
mmr_l0.3     True    1.0     0.75      2  0.5289
mmr_l0.5     True    1.0     0.75      2  0.5289
mmr_l0.7     True    1.0     0.75      2  0.5289
mmr_l1.0     True    1.0     0.75      2  0.5289

[数1 充电宝] 候选 20 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0      1.0      1  0.3999
greedy       True    1.0      1.0      1  0.3999
mmr_l0.3     True    1.0      1.0      1  0.4332
mmr_l0.5     True    1.0      1.0      1  0.4332
mmr_l0.7     True    1.0      1.0      1  0.4332
mmr_l1.0     True    1.0      1.0      1  0.3999

[数2 键盘] 候选 19 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0      1.0      1  0.0847
greedy       True    1.0      1.0      1  0.0847
mmr_l0.3     True    1.0      1.0      1  0.0682
mmr_l0.5     True    1.0      1.0      1  0.0682
mmr_l0.7     True    1.0      1.0      1  0.0682
mmr_l1.0     True    1.0      1.0      1  0.0847

[数3 鼠标] 候选 20 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0      1.0      1  1.4019
greedy       True    1.0      1.0      1  1.4019
mmr_l0.3     True    1.0      1.0      1  1.4019
mmr_l0.5     True    1.0      1.0      1  1.4019
mmr_l0.7     True    1.0      1.0      1  1.4019
mmr_l1.0     True    1.0      1.0      1  1.4019

[数4 数据线] 候选 19 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0      1.0      2  0.2108
greedy       True    1.0      1.0      2  0.2108
mmr_l0.3     True    1.0      1.0      2  0.2108
mmr_l0.5     True    1.0      1.0      2  0.2108
mmr_l0.7     True    1.0      1.0      2  0.2108
mmr_l1.0     True    1.0      1.0      2  0.2108

[生1 马克杯] 候选 20 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0     0.75      1  0.5629
greedy       True    1.0     0.75      1  0.5629
mmr_l0.3     True    1.0     0.75      2  0.5212
mmr_l0.5     True    1.0     0.75      2  0.5212
mmr_l0.7     True    1.0     0.75      2  0.5212
mmr_l1.0     True    1.0     0.75      1  0.5629

[生2 雨伞] 候选 20 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0     0.75      1  0.7719
greedy       True    1.0     0.75      1  0.7719
mmr_l0.3     True    1.0     0.75      1  0.8609
mmr_l0.5     True    1.0     0.75      1  0.8609
mmr_l0.7     True    1.0     0.75      1  0.8609
mmr_l1.0     True    1.0     0.75      1  0.7719

[生3 帆布包] 候选 20 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0      1.0      2  1.4637
greedy       True    1.0      1.0      2  1.4637
mmr_l0.3     True    1.0   0.8333      3  1.0843
mmr_l0.5     True    1.0   0.8333      3  1.0843
mmr_l0.7     True    1.0   0.8333      3  1.0843
mmr_l1.0     True    1.0      1.0      2  1.4637

[生4 围巾] 候选 20 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0      0.6      1  0.8832
greedy       True    1.0      0.6      1  0.8832
mmr_l0.3     True    1.0      0.8      1  0.8741
mmr_l0.5     True    1.0      0.8      1  0.8741
mmr_l0.7     True    1.0      0.8      1  0.8741
mmr_l1.0     True    1.0      0.6      1  0.8832

[团1 通勤外套] 候选 11 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0   0.8333      2  0.2427
greedy       True    1.0   0.8333      2  0.2427
mmr_l0.3     True    1.0   0.8333      3  0.2783
mmr_l0.5     True    1.0   0.8333      3  0.2783
mmr_l0.7     True    1.0   0.8333      3  0.2783
mmr_l1.0     True    1.0   0.8333      2  0.2427

[团2 蓝牙耳机] 候选 16 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0   0.7143      3  0.1457
greedy       True    1.0   0.7143      3  0.1457
mmr_l0.3     True    1.0   0.7143      3  0.4718
mmr_l0.5     True    1.0   0.7143      3  0.4718
mmr_l0.7     True    1.0   0.7143      3  0.4718
mmr_l1.0     True    1.0   0.7143      3  0.1457

[团3 双肩背包] 候选 18 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0      1.0      1  0.2877
greedy       True    1.0      1.0      1  0.2877
mmr_l0.3     True    1.0      1.0      1  0.2877
mmr_l0.5     True    1.0      1.0      1  0.2877
mmr_l0.7     True    1.0      1.0      1  0.2877
mmr_l1.0     True    1.0      1.0      1  0.2877

[团4 保温杯] 候选 20 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0      1.0      3  0.3559
greedy       True    1.0      1.0      3  0.3559
mmr_l0.3     True    1.0   0.8333      2  0.4443
mmr_l0.5     True    1.0   0.8333      2  0.4443
mmr_l0.7     True    1.0   0.8333      2  0.4443
mmr_l1.0     True    1.0      1.0      3  0.3559

[团5 雨伞] 候选 5 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0   0.8333      2  0.2721
greedy       True    1.0   0.8333      2  0.2721
mmr_l0.3     True    1.0   0.8333      2  0.2714
mmr_l0.5     True    1.0   0.8333      2  0.2714
mmr_l0.7     True    1.0   0.8333      2  0.2714
mmr_l1.0     True    1.0   0.8333      2  0.2721

[团6 针织围巾] 候选 9 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0   0.8333      1  0.3589
greedy       True    1.0   0.8333      1  0.3589
mmr_l0.3     True    1.0   0.8333      1  0.3589
mmr_l0.5     True    1.0   0.8333      1  0.3589
mmr_l0.7     True    1.0   0.8333      1  0.3589
mmr_l1.0     True    1.0   0.8333      1  0.3589

[团7 静音键盘] 候选 15 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0   0.6667      2  0.9723
greedy       True    1.0   0.6667      2  0.9723
mmr_l0.3     True    1.0   0.6667      2  0.9723
mmr_l0.5     True    1.0   0.6667      2  0.9723
mmr_l0.7     True    1.0   0.6667      2  0.9723
mmr_l1.0     True    1.0   0.6667      2  0.9723

[团8 帆布托特包] 候选 3 件
策略            Hit    MRR     可判定率    多样性    价格CV
topk_raw     True    1.0   0.8333      1  0.3711
greedy       True    1.0   0.8333      1  0.3711
mmr_l0.3     True    1.0   0.8333      1  0.3711
mmr_l0.5     True    1.0   0.8333      1  0.3711
mmr_l0.7     True    1.0   0.8333      1  0.3711
mmr_l1.0     True    1.0   0.8333      1  0.3711

====================================================================================================
汇总（均值，k=3）：
策略             Hit率    MRR     可判定率    多样性    价格CV
topk_raw        1.0    1.0   0.8682    1.5  0.5393
greedy          1.0    1.0   0.8682    1.5  0.5393
mmr_l0.3        1.0    1.0   0.8615   1.75  0.5583
mmr_l0.5        1.0    1.0   0.8615   1.75  0.5583
mmr_l0.7        1.0    1.0   0.8615   1.75  0.5583
mmr_l1.0        1.0    1.0   0.8682    1.5  0.5393

读法：topk_raw=模拟 Channel3 原序；greedy=只按相关度排序；mmr_l0.3/l0.5/l0.7/l1.0=MMR 重排 λ 扫描。Hit/MRR/可判定率全部来自生产判定函数 evaluate（零人工标注）；多样性=top-k 唯一品类数，价格CV=价格离散度（MMR 的本职是抑制扎堆）。
注意：MRR 的 rank 是「第一个无 ❌ 商品」的位置——组合类约束（数量/总预算）
对单件天然 unknown，这一行如实反映判定层的可判定边界，不是缺陷。
