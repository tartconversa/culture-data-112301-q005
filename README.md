# 文化素材许可的用量解释与续约承保

用于本地业务开发的合成经营资料，所有参与方均使用虚构标识。

data保存UTF-8 JSONL记录及catalog.json格式登记，docs/资料契约.md说明来源口径、字段、单位及直接引用。不同来源、业务时期与后续到达的记录各自保留身份，不能仅按文件行数解释业务数量。

Python需要3.11至3.14。依赖版本在requirements.txt中固定，可在独立虚拟环境安装。

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 tools/check_materials.py
python3 -m unittest discover -s tests -v
```

Windows PowerShell中可用`.venv\Scripts\Activate.ps1`激活虚拟环境。

资料检查入口输出各表行数，验证内容指纹、字段类型及直接外键。现有测试检查资料完整性和损坏检测，其通过只说明资料可读取。data及docs中的记录不包含经营分析结果。应用产生的本地状态可放入state，导出内容可放入outputs，两者已从Git排除。

## 业务应用

`app/` 把使用证据转成可解释的许可消耗与续约方案，仅依赖Python标准库。因隔离模式（`-I`）不把当前目录加入导入路径，命令行通过 `tools/run_app.py` 运行：

```sh
python3 -I tools/run_app.py consumption     # 去重后的逐条许可消耗与冲突
python3 -I tools/run_app.py disputes        # 未授权/证据不足/覆盖分歧清单
python3 -I tools/run_app.py batches         # 完整观察批次与仍在观察批次
python3 -I tools/run_app.py reconcile       # 季度声明对账（按最新修订）
python3 -I tools/run_app.py request Q0      # 续约方案比较、授权与担保评估
python3 -I tools/run_app.py issue Q0 guarantee+usage --on 2026-11-01
python3 -I tools/run_app.py guarantees      # 共享担保池额度与报价占用
python3 -I tools/run_app.py amendments      # 声明修正、归属裁决、范围缩小的影响
python3 -I tools/run_app.py reset           # 清空state/，原始资料不受影响
```

应用测试（含资料检查，共56个用例）：

```sh
python3 -I -m unittest discover -s tests -v
```

规则与扩展方式见 `docs/应用架构.md`，新增或替换数据的步骤见 `docs/数据接入指南.md`。
