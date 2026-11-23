"""文化素材许可的用量解释与续约承保应用。

模块划分：

- models: 资料行与业务结果的数据类
- store: 合成资料读取、指纹校验与本地状态持久化
- lineage: 衍生谱系与归属裁决
- evidence: 使用事件去重、证据效力与许可消耗
- batches: 发行批次观察状态
- statements_reconcile: 季度声明修订与对账
- pricing: 续约条款组合与计价
- quoting: 报价生命周期、授权校验与担保占用
- amendments: 声明修正、归属裁决与发行范围缩小的影响判断
"""

__all__ = ["store", "models"]
