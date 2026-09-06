"""一次性人工减仓：不改策略/风险配置，不自动恢复未确认订单。调用方必须持有交易锁。"""
import logging
import uuid

from config_validation import strict_float_finite

logger = logging.getLogger(__name__)


def preview_reduction(system, symbol, percent):
    if isinstance(percent, bool):
        raise ValueError('减仓比例必须是数字，不接受布尔值')
    percent = strict_float_finite(percent, '减仓比例')
    if not 0 < percent < 100:
        raise ValueError('减仓比例必须大于 0、小于 100；全部退出请使用全平')
    position = system.trade_state.get_open_position(symbol)
    if not position or not position.get('open_time'):
        raise ValueError('没有可识别的托管持仓')
    if not position.get('stop_order_id'):
        raise ValueError('缺少可识别的原止损单，拒绝开始减仓')
    if 'pending_reduction' in position or system.trade_state.has_stop_residue(symbol):
        raise ValueError('该品种有待人工核对的操作或止损残留，拒绝减仓')
    api = system.exchange_api
    actual = api.get_position(symbol)
    if not actual or not api.managed_position_matches(
            symbol, actual, position['side'], position['position_size']):
        raise ValueError('交易所实际仓位与账本不一致，拒绝减仓，请人工核对')
    if not api.partial_stop_is_intact(symbol, position['side'], position['position_size'],
                                      position['stop_loss_price'], position['stop_order_id']):
        raise ValueError('无法确认现有止损完整，拒绝开始减仓')
    quote = api.preview_partial_close(symbol, actual, percent)
    quote.update(symbol=symbol, side=position['side'], percent=percent,
                 original_size=position['position_size'], open_time=position['open_time'],
                 stop_price=position['stop_loss_price'], stop_order_id=position.get('stop_order_id'),
                 revision=position.get('reduction_revision', 0), client_order_id=uuid.uuid4().hex)
    return quote


def execute_reduction(system, quote):
    """下单后任何不确定性保留 pending；人工裁决前不再向该品种发送交易指令。"""
    symbol = quote['symbol']
    # 签名预览只证明用户确认过；下单前还需重新核对行情、实仓、止损和元数据。
    fresh = preview_reduction(system, symbol, quote['percent'])
    fields = ('side', 'open_time', 'original_size', 'revision', 'stop_price',
              'stop_order_id', 'contracts', 'original_contracts', 'contract_size', 'reduce_size')
    if any(fresh[key] != quote[key] for key in fields):
        raise ValueError('预览已过期或仓位/交易规则已变化，请刷新后重新确认')
    system.trade_state.begin_reduction(symbol, quote)
    try:
        result = system.exchange_api.execute_partial_close(symbol, quote)
        trade = system.trade_state.finish_reduction(
            symbol, quote['client_order_id'], result['filled_size'],
            result['remaining_size'], result['average'])
    except Exception:
        logger.exception('%s 减仓未完成核对，保留持久化隔离记录', symbol)
        system._pending_reduction_blocks_management(symbol)
        raise RuntimeError(f'{symbol} 减仓结果或止损尚未确认，已隔离；勿重复操作，请人工核对') from None
    position = system.trade_state.get_open_position(symbol)
    try:
        system.notifier.send_message(
            '交易系统人工减仓结果',
            f"{symbol} 本次实际平仓 {result['filled_size']}，剩余 {position['position_size']}\n"
            f"止损价仍为 {position['stop_loss_price']}；不会自动补回，开仓风险度未修改\n"
            f"操作号: {quote['client_order_id']}")
    except Exception as e:
        logger.warning('减仓结果推送失败（不改变已确认的成交结果）: %s', e)
    return {'status': 'success', 'filled_size': result['filled_size'],
            'remaining_size': position['position_size'], 'trade': trade,
            'message': '已按实际成交量更新托管；未成交部分不会自动补单'}
