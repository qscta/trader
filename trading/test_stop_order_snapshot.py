"""止损算法单批量快照：只优化成功读路径，不改变四态裁决与失败语义。"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import _test_stubs

TradingSystem = _test_stubs.import_main().TradingSystem


class StopOrderSnapshotTest(unittest.TestCase):
    @staticmethod
    def _symbol(symbol):
        return symbol[:-4] + '/USDT:USDT' if symbol.endswith('USDT') else symbol

    def _system(self):
        system = TradingSystem.__new__(TradingSystem)
        system._stop_anomalies = {}
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=self._symbol,
            fetch_stop_order_snapshot=Mock(),
            find_stop_order_state=Mock(return_value='intact'),
        )
        system.trade_state = SimpleNamespace(
            has_stop_residue=Mock(return_value=False),
            get_open_position=Mock(),
        )
        return system

    @staticmethod
    def _positions():
        return {
            'BTCUSDT': {
                'side': 'long', 'position_size': 1.0,
                'stop_loss_price': 90.0, 'stop_order_id': 'btc-stop',
            },
            'ETHUSDT': {
                'side': 'short', 'position_size': 2.0,
                'stop_loss_price': 120.0, 'stop_order_id': 'eth-stop',
            },
        }

    def test_one_complete_snapshot_is_shared_by_all_requested_symbols(self):
        system = self._system()
        expected = {
            'BTC/USDT:USDT': ({'id': 'btc-stop'},),
            'ETH/USDT:USDT': ({'id': 'eth-stop'},),
        }
        system.exchange_api.fetch_stop_order_snapshot.return_value = expected

        snapshot = system._load_stop_order_snapshot(
            self._positions(), '测试巡检')

        self.assertEqual(expected, snapshot.orders)
        system.exchange_api.fetch_stop_order_snapshot.assert_called_once_with(
            ['BTC/USDT:USDT', 'ETH/USDT:USDT'])

    def test_expired_snapshot_falls_back_to_live_per_symbol_query(self):
        system = self._system()
        expected = {
            'BTC/USDT:USDT': ({'id': 'btc-stop'},),
            'ETH/USDT:USDT': ({'id': 'eth-stop'},),
        }
        system.exchange_api.fetch_stop_order_snapshot.return_value = expected
        snapshot = system._load_stop_order_snapshot(
            self._positions(), '测试巡检')

        with patch(
                'stop_guardian.time.monotonic',
                return_value=(snapshot.started_at +
                              system.STOP_ORDER_SNAPSHOT_MAX_AGE_SECONDS + 0.01)):
            self.assertIsNone(system._orders_from_stop_snapshot(
                snapshot, 'BTC/USDT:USDT'))

        system.exchange_api.find_stop_order_state.return_value = 'intact'
        self.assertTrue(system._ensure_stop_order_alive(
            'BTCUSDT', 'BTC/USDT:USDT', self._positions()['BTCUSDT'],
            '测试策略', algo_orders=None))
        self.assertIsNone(
            system.exchange_api.find_stop_order_state.call_args.kwargs[
                'algo_orders'])

    def test_expired_snapshot_is_refreshed_once_for_all_remaining_symbols(self):
        system = self._system()
        first_orders = {
            'BTC/USDT:USDT': ({'id': 'btc-old'},),
            'ETH/USDT:USDT': ({'id': 'eth-old'},),
        }
        refreshed_orders = {
            'BTC/USDT:USDT': ({'id': 'btc-new'},),
            'ETH/USDT:USDT': ({'id': 'eth-new'},),
        }
        system.exchange_api.fetch_stop_order_snapshot.return_value = first_orders
        snapshot = system._load_stop_order_snapshot(
            self._positions(), '测试巡检')
        system.exchange_api.fetch_stop_order_snapshot.return_value = (
            refreshed_orders)
        base = snapshot.started_at
        after_expiry = (
            base + system.STOP_ORDER_SNAPSHOT_MAX_AGE_SECONDS + 1)

        with patch(
                'stop_guardian.time.monotonic',
                side_effect=(
                    after_expiry, after_expiry + 0.1,
                    after_expiry + 0.2, after_expiry + 0.2,
                    after_expiry + 0.2)):
            current = system._refresh_stop_order_snapshot_if_expired(
                snapshot, self._positions(), '日检')
            # 第二个品种复用同一份新快照，不触发第三轮批量读取。
            current = system._refresh_stop_order_snapshot_if_expired(
                current, self._positions(), '日检')

        self.assertEqual(
            system.exchange_api.fetch_stop_order_snapshot.call_args_list,
            [
                call(
                    ['BTC/USDT:USDT', 'ETH/USDT:USDT']),
                call(
                    ['BTC/USDT:USDT', 'ETH/USDT:USDT']),
            ])
        self.assertEqual(
            system._orders_from_stop_snapshot(
                current, 'BTC/USDT:USDT'),
            refreshed_orders['BTC/USDT:USDT'])
        self.assertEqual(
            system._orders_from_stop_snapshot(
                current, 'ETH/USDT:USDT'),
            refreshed_orders['ETH/USDT:USDT'])

    def test_failed_or_already_stale_batch_refresh_is_sticky_fallback(self):
        system = self._system()
        expected = {
            'BTC/USDT:USDT': ({'id': 'btc-old'},),
            'ETH/USDT:USDT': ({'id': 'eth-old'},),
        }
        system.exchange_api.fetch_stop_order_snapshot.return_value = expected
        snapshot = system._load_stop_order_snapshot(
            self._positions(), '测试巡检')
        expired_at = (
            snapshot.started_at +
            system.STOP_ORDER_SNAPSHOT_MAX_AGE_SECONDS + 1)

        with self.subTest('refresh failure'), patch(
                'stop_guardian.time.monotonic',
                return_value=expired_at), patch.object(
                    system, '_load_stop_order_snapshot',
                    return_value=None) as reload_snapshot:
            current = system._refresh_stop_order_snapshot_if_expired(
                snapshot, self._positions(), '日检')
            current = system._refresh_stop_order_snapshot_if_expired(
                current, self._positions(), '日检')
            self.assertIsNone(current)
            reload_snapshot.assert_called_once()

        stale_refresh = SimpleNamespace(
            orders=expected, started_at=snapshot.started_at)
        with self.subTest('slow refresh'), patch(
                'stop_guardian.time.monotonic',
                return_value=expired_at), patch.object(
                    system, '_load_stop_order_snapshot',
                    return_value=stale_refresh) as reload_snapshot:
            current = system._refresh_stop_order_snapshot_if_expired(
                snapshot, self._positions(), '日检')
            current = system._refresh_stop_order_snapshot_if_expired(
                current, self._positions(), '日检')
            self.assertIsNone(current)
            reload_snapshot.assert_called_once()

    def test_partial_or_malformed_snapshot_falls_back_instead_of_assuming_empty(self):
        system = self._system()
        system.exchange_api.fetch_stop_order_snapshot.return_value = {
            'BTC/USDT:USDT': (),
        }

        self.assertIsNone(system._load_stop_order_snapshot(
            self._positions(), '测试巡检'))

    def test_snapshot_query_failure_falls_back_to_per_symbol_path(self):
        system = self._system()
        system.exchange_api.fetch_stop_order_snapshot.side_effect = RuntimeError(
            'one algo type unavailable')

        self.assertIsNone(system._load_stop_order_snapshot(
            self._positions(), '测试巡检'))

    def test_classifier_receives_shared_snapshot_without_requery(self):
        system = self._system()
        position = self._positions()['BTCUSDT']
        orders = ({'id': 'btc-stop'},)

        self.assertTrue(system._ensure_stop_order_alive(
            'BTCUSDT', 'BTC/USDT:USDT', position, '测试策略',
            algo_orders=orders))

        system.exchange_api.find_stop_order_state.assert_called_once_with(
            'BTC/USDT:USDT', 'long', 1.0, 90.0, 'btc-stop',
            algo_orders=orders)

    def test_resize_invalidates_pre_resize_snapshot(self):
        system = self._system()
        position = dict(self._positions()['BTCUSDT'], stop_resize_pending=True)
        resized = dict(position, stop_resize_pending=False)
        system._retry_partial_stop_resize = Mock(return_value=True)
        system.trade_state.get_open_position.return_value = resized

        self.assertTrue(system._ensure_stop_order_alive(
            'BTCUSDT', 'BTC/USDT:USDT', position, '测试策略',
            algo_orders=({'id': 'old-stop'},)))

        self.assertIsNone(
            system.exchange_api.find_stop_order_state.call_args.kwargs[
                'algo_orders'])


class CompensationContractTest(unittest.TestCase):
    def test_compensation_close_always_uses_derived_client_order_id(self):
        system = TradingSystem.__new__(TradingSystem)
        system.exchange_api = SimpleNamespace(
            compensation_client_order_id=Mock(return_value='Rstable'),
            close_position=Mock(return_value={'id': 'close-1'}),
        )

        result = system._submit_compensation_close(
            'BTC/USDT:USDT', 'long', 1.0,
            open_client_order_id='Istable')

        self.assertEqual({'id': 'close-1'}, result)
        system.exchange_api.compensation_client_order_id.assert_called_once_with(
            'Istable')
        system.exchange_api.close_position.assert_called_once_with(
            'BTC/USDT:USDT', 'long', 1.0,
            client_order_id='Rstable')

    def test_missing_compensation_id_contract_never_posts_unkeyed_close(self):
        system = TradingSystem.__new__(TradingSystem)
        close_position = Mock()
        system.exchange_api = SimpleNamespace(close_position=close_position)

        with self.assertRaises(AttributeError):
            system._submit_compensation_close(
                'BTC/USDT:USDT', 'long', 1.0,
                open_client_order_id='Istable')

        close_position.assert_not_called()


if __name__ == '__main__':
    unittest.main()
