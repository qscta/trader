"""测试共用：在临时桩环境下导入 main，避免引入 ccxt/apscheduler/requests 等第三方依赖。

用法（在测试模块顶部）::

    import _test_stubs
    TradingSystem = _test_stubs.import_main().TradingSystem

桩模块只在 import_main() 执行期间存在于 sys.modules，导入完成后立即恢复原状，
因此同一进程内的其它测试模块（无论导入顺序）拿到的都是真实模块，互不污染。
trade_state / equity_tracker 是纯标准库实现，不桩，main 直接绑定真实现。
"""
import importlib
import logging
import sys
import types


class Dummy:
    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        # 任意方法调用返回 None（让 BackgroundScheduler().add_job(...) 这类装配调用可空转）
        return lambda *args, **kwargs: None


def _base_exchange_stub(*_args, **_kwargs):
    raise NotImplementedError


_EXCHANGE_API_METHODS = (
    'to_ccxt_symbol', 'get_position', 'list_position_symbols',
    'verify_one_way_mode', 'setup_symbol', 'open_position', 'close_position',
    'compensation_client_order_id', 'create_stop_loss_order',
    'cancel_stop_order_only', 'cancel_order', 'cancel_all_orders',
    'round_quantity', 'get_quantity_precision', 'find_stop_order_state',
    'find_existing_open_order', 'find_compensation_close_progress',
    'confirm_stop_execution',
)
ExchangeApiStub = type(
    'ExchangeApi', (),
    {name: _base_exchange_stub for name in _EXCHANGE_API_METHODS},
)


_STUB_SPECS = (
    ('apscheduler', {}),
    ('apscheduler.schedulers', {}),
    ('apscheduler.schedulers.background', {'BackgroundScheduler': Dummy}),
    # main imports this class directly for its startup capability-closure
    # check.  Stubbing only okx_api left the supposedly stdlib-only suite
    # dependent on whichever earlier test happened to cache exchange_base.
    ('exchange_base', {'ExchangeApi': ExchangeApiStub}),
    ('ma_cross_strategy', {'MaCrossStrategy': Dummy}),
    ('risk_manager', {'RiskManager': Dummy}),
    ('dingtalk_notifier', {'DingTalkNotifier': Dummy}),  # 避免 requests
    ('okx_api', {'OkxApi': Dummy}),                      # 避免 ccxt
)


def import_main():
    """桩接依赖→导入 main→立即恢复 sys.modules，返回导入的 main 模块。"""
    # 测试进程不落生产日志：main 导入时的 logging.basicConfig(handlers=[RotatingFileHandler
    # (trading.log), StreamHandler]) 仅在根 logger 无 handler 时生效——先挂 NullHandler
    # 让它空转。否则在部署机上跑测试会把模拟场景（止损残留/CRITICAL 告警等）写进真实
    # trading.log，污染监控视野还会造成真假告警混淆。
    root_logger = logging.getLogger()
    if not root_logger.handlers:
        root_logger.addHandler(logging.NullHandler())
    saved = {}
    for name, attrs in _STUB_SPECS:
        saved[name] = sys.modules.get(name)
        mod = types.ModuleType(name)
        for attr, value in attrs.items():
            setattr(mod, attr, value)
        sys.modules[name] = mod
    saved_main = sys.modules.pop('main', None)
    try:
        main = importlib.import_module('main')
    finally:
        for name, orig in saved.items():
            if orig is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = orig
        # 本次导入的 main 绑定了桩，不能留在缓存里给别人；原有的 main(若存在)放回
        sys.modules.pop('main', None)
        if saved_main is not None:
            sys.modules['main'] = saved_main
    return main
