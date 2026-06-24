"""Executor 集成测试 - 验证 DSL、Python、Preset 三条执行路径的端到端流程"""

import pytest
from datetime import date
from forecast.models.forecast import ForecastInput, TimeSeriesPoint
from forecast.core.executor import execute_skill, SandboxError


class TestExecutorDSLPath:
    """DSL 执行路径测试"""

    def test_dsl_moving_average(self):
        """测试 DSL 移动平均函数"""
        input_data = ForecastInput(
            carModel="TestCar",
            color="Red",
            demand=[
                TimeSeriesPoint(date=date(2026, 1, 1), qty=100),
                TimeSeriesPoint(date=date(2026, 1, 2), qty=120),
                TimeSeriesPoint(date=date(2026, 1, 3), qty=110),
                TimeSeriesPoint(date=date(2026, 1, 4), qty=130),
                TimeSeriesPoint(date=date(2026, 1, 5), qty=115),
            ],
            pgi=[],
            beginning_inventory=500
        )

        result = execute_skill(
            skill_type="dsl",
            dsl_expression="moving_average(demand, 3)",
            python_code=None,
            preset_name=None,
            input_data=input_data
        )

        assert "TestCar" in str(result.model_extra)
        assert "Red" in str(result.model_extra)
        assert len(result.forecast) > 0
        assert all(isinstance(pt.qty, float) for pt in result.forecast)
        assert result.metadata.get("method") == "dsl"

    def test_dsl_complex_expression(self):
        """测试复杂 DSL 表达式（组合多个函数）"""
        input_data = ForecastInput(
            carModel="TestCar",
            color="Blue",
            demand=[
                TimeSeriesPoint(date=date(2026, 1, i), qty=100 + i * 10)
                for i in range(1, 11)
            ],
            pgi=[],
            beginning_inventory=300
        )

        # 线性趋势 + 移动平均的组合
        result = execute_skill(
            skill_type="dsl",
            dsl_expression="linear_trend(demand, 5) + moving_average(demand, 3)",
            python_code=None,
            preset_name=None,
            input_data=input_data
        )

        assert len(result.forecast) > 0
        assert all(pt.qty >= 0 for pt in result.forecast)

    def test_dsl_empty_demand(self):
        """测试空需求数据的 DSL 执行"""
        input_data = ForecastInput(
            carModel="TestCar",
            color="Green",
            demand=[],
            pgi=[],
            beginning_inventory=0
        )

        # 空数据应该返回空预测
        result = execute_skill(
            skill_type="dsl",
            dsl_expression="moving_average(demand, 3)",
            python_code=None,
            preset_name=None,
            input_data=input_data
        )

        assert "TestCar" in str(result.model_extra)
        assert len(result.forecast) == 0


class TestExecutorPythonPath:
    """Python 执行路径测试"""

    def test_python_simple_forecast(self):
        """测试简单 Python 预测函数"""
        input_data = ForecastInput(
            carModel="PyCar",
            color="Black",
            demand=[
                TimeSeriesPoint(date=date(2026, 1, 1), qty=100),
                TimeSeriesPoint(date=date(2026, 1, 2), qty=110),
                TimeSeriesPoint(date=date(2026, 1, 3), qty=105),
            ],
            pgi=[],
            beginning_inventory=200
        )

        python_code = """
def forecast(record):
    demand = record.get('demand', [])
    if not demand:
        return []
    avg = sum(d['qty'] for d in demand) / len(demand)
    return [{'date': '2026-01-04', 'qty': avg}]
"""

        result = execute_skill(
            skill_type="python",
            dsl_expression=None,
            python_code=python_code,
            preset_name=None,
            input_data=input_data
        )

        assert "PyCar" in str(result.model_extra)
        assert len(result.forecast) == 1
        assert result.forecast[0].qty == pytest.approx(105.0)
        assert result.metadata.get("method") == "python_or_preset"

    def test_python_complex_logic(self):
        """测试复杂 Python 逻辑（包含条件判断）"""
        input_data = ForecastInput(
            carModel="PyCar",
            color="White",
            demand=[
                TimeSeriesPoint(date=date(2026, 1, i), qty=50 * i)
                for i in range(1, 6)
            ],
            pgi=[],
            beginning_inventory=100
        )

        python_code = """
def forecast(record):
    demand = record.get('demand', [])
    begin_inv = record.get('beginningInventory', 0)

    if not demand:
        return []

    total_demand = sum(d['qty'] for d in demand)
    net_need = max(0, total_demand - begin_inv)

    return [{'date': '2026-01-06', 'qty': net_need}]
"""

        result = execute_skill(
            skill_type="python",
            dsl_expression=None,
            python_code=python_code,
            preset_name=None,
            input_data=input_data
        )

        assert len(result.forecast) == 1
        # 总需求 = 50+100+150+200+250 = 750, 净需求 = 750-100 = 650
        assert result.forecast[0].qty == 650.0

    def test_python_sandbox_security(self):
        """测试 Python 沙箱安全性（禁止危险操作）"""
        input_data = ForecastInput(
            carModel="HackCar",
            color="Gray",
            demand=[TimeSeriesPoint(date=date(2026, 1, 1), qty=100)],
            pgi=[],
            beginning_inventory=0
        )

        # 尝试访问 __builtins__（应该被拒绝）
        malicious_code = """
def forecast(record):
    import os
    return os.system('echo hacked')
"""

        with pytest.raises(SandboxError):
            execute_skill(
                skill_type="python",
                dsl_expression=None,
                python_code=malicious_code,
                preset_name=None,
                input_data=input_data
            )

    def test_python_no_forecast_function(self):
        """测试缺少 forecast 函数的情况"""
        input_data = ForecastInput(
            carModel="TestCar",
            color="Yellow",
            demand=[TimeSeriesPoint(date=date(2026, 1, 1), qty=100)],
            pgi=[],
            beginning_inventory=0
        )

        bad_code = """
def calculate(record):
    return []
"""

        with pytest.raises(Exception):  # 应该抛出执行错误
            execute_skill(
                skill_type="python",
                dsl_expression=None,
                python_code=bad_code,
                preset_name=None,
                input_data=input_data
            )


class TestExecutorPresetPath:
    """Preset 执行路径测试"""

    def test_preset_moving_average(self):
        """测试 moving_average 预设"""
        input_data = ForecastInput(
            carModel="PresetCar",
            color="Silver",
            demand=[
                TimeSeriesPoint(date=date(2026, 1, i), qty=100 + i * 5)
                for i in range(1, 8)
            ],
            pgi=[],
            beginning_inventory=300
        )

        result = execute_skill(
            skill_type="preset",
            dsl_expression=None,
            python_code=None,
            preset_name="moving_average",
            input_data=input_data
        )

        assert "PresetCar" in str(result.model_extra)
        assert len(result.forecast) > 0
        assert result.metadata.get("method") == "python_or_preset"

    def test_preset_jitcall_priority(self):
        """测试 JITCall 优先级业务逻辑预设"""
        input_data = ForecastInput(
            carModel="FWDYCar",
            color="Gold",
            demand=[
                TimeSeriesPoint(date=date(2026, 1, i), qty=100)
                for i in range(1, 8)
            ],
            pgi=[TimeSeriesPoint(date=date(2026, 1, 1), qty=50)],
            beginning_inventory=200,
            weekly_demand=800
        )
        # 手动添加 jitcall 字段
        input_data.jitcall = [
            TimeSeriesPoint(date=date(2026, 1, 3), qty=80),
            TimeSeriesPoint(date=date(2026, 1, 5), qty=80),
        ]

        result = execute_skill(
            skill_type="preset",
            dsl_expression=None,
            python_code=None,
            preset_name="jitcall_priority",
            input_data=input_data
        )

        assert len(result.forecast) > 0
        assert all(pt.qty >= 0 for pt in result.forecast)

    def test_preset_invalid_name(self):
        """测试无效的预设名称"""
        input_data = ForecastInput(
            carModel="TestCar",
            color="Pink",
            demand=[TimeSeriesPoint(date=date(2026, 1, 1), qty=100)],
            pgi=[],
            beginning_inventory=0
        )

        with pytest.raises(Exception):  # 应该抛出未知预设错误
            execute_skill(
                skill_type="preset",
                dsl_expression=None,
                python_code=None,
                preset_name="nonexistent_preset",
                input_data=input_data
            )

    def test_preset_with_pgi(self):
        """测试包含 PGI 数据的预设执行"""
        input_data = ForecastInput(
            carModel="PGICar",
            color="Orange",
            demand=[
                TimeSeriesPoint(date=date(2026, 1, i), qty=100)
                for i in range(1, 6)
            ],
            pgi=[
                TimeSeriesPoint(date=date(2026, 1, 1), qty=30),
                TimeSeriesPoint(date=date(2026, 1, 2), qty=40),
            ],
            beginning_inventory=150
        )

        result = execute_skill(
            skill_type="preset",
            dsl_expression=None,
            python_code=None,
            preset_name="moving_average",
            input_data=input_data
        )

        assert len(result.forecast) > 0


class TestExecutorEdgeCases:
    """边界情况测试"""

    def test_invalid_skill_config(self):
        """测试无效的技能配置"""
        input_data = ForecastInput(
            carModel="TestCar",
            color="White",
            demand=[TimeSeriesPoint(date=date(2026, 1, 1), qty=100)],
            pgi=[],
            beginning_inventory=0
        )

        # DSL 类型但没有表达式
        with pytest.raises(ValueError):
            execute_skill(
                skill_type="dsl",
                dsl_expression=None,
                python_code=None,
                preset_name=None,
                input_data=input_data
            )

        # Python 类型但没有代码
        with pytest.raises(ValueError):
            execute_skill(
                skill_type="python",
                dsl_expression=None,
                python_code=None,
                preset_name=None,
                input_data=input_data
            )

        # Preset 类型但没有名称
        with pytest.raises(ValueError):
            execute_skill(
                skill_type="preset",
                dsl_expression=None,
                python_code=None,
                preset_name=None,
                input_data=input_data
            )

    def test_single_point_demand(self):
        """测试单个数据点的需求"""
        input_data = ForecastInput(
            carModel="SingleCar",
            color="Violet",
            demand=[TimeSeriesPoint(date=date(2026, 1, 1), qty=100)],
            pgi=[],
            beginning_inventory=0
        )

        result = execute_skill(
            skill_type="dsl",
            dsl_expression="mean(demand)",
            python_code=None,
            preset_name=None,
            input_data=input_data
        )

        assert len(result.forecast) == 1
        assert result.forecast[0].qty == 100.0

    def test_metadata_correctness(self):
        """测试元数据正确性"""
        input_data = ForecastInput(
            carModel="MetaCar",
            color="Brown",
            demand=[
                TimeSeriesPoint(date=date(2026, 1, i), qty=100)
                for i in range(1, 4)
            ],
            pgi=[],
            beginning_inventory=0
        )

        # DSL 路径
        dsl_result = execute_skill(
            skill_type="dsl",
            dsl_expression="mean(demand)",
            python_code=None,
            preset_name=None,
            input_data=input_data
        )
        assert dsl_result.metadata["method"] == "dsl"

        # Python 路径
        python_result = execute_skill(
            skill_type="python",
            dsl_expression=None,
            python_code="def forecast(r): return [{'date': '2026-01-04', 'qty': 100}]",
            preset_name=None,
            input_data=input_data
        )
        assert python_result.metadata["method"] == "python_or_preset"

        # Preset 路径
        preset_result = execute_skill(
            skill_type="preset",
            dsl_expression=None,
            python_code=None,
            preset_name="moving_average",
            input_data=input_data
        )
        assert preset_result.metadata["method"] == "python_or_preset"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
