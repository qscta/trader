"""调度/状态机审计回归（纯标准库）。"""

import copy
import json
import os
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import _test_stubs

main = _test_stubs.import_main()
TradingSystem = main.TradingSystem
from trade_state import TradeState, TradeStatePersistenceError  # noqa: E402


def _absent_compensation_progress(
        client_order_id, requested_amount):
    """Mirror OkxApi's exact read-only absent contract."""
    return {
        'terminal': None, 'absent': True, 'confirmed': False,
        'filled': 0.0, 'amount': 0.0,
        'requested_amount': float(requested_amount),
        'remaining_amount': float(requested_amount),
        'clientOrderId': client_order_id,
        'ids': [], 'read_only_evidence': True, 'order': None,
        'order_state': {
            'client_order_id': client_order_id,
            'presence': 'absent', 'terminal': None, 'filled': None,
        },
    }


def _terminal_compensation_progress(
        client_order_id, requested_contracts, requested_amount,
        filled_contracts, *, order_id='compensation-order',
        average=99.0):
    """Mirror OkxApi's exact present-terminal single-order contract."""
    requested_contracts = float(requested_contracts)
    requested_amount = float(requested_amount)
    filled_contracts = float(filled_contracts)
    remaining_contracts = requested_contracts - filled_contracts
    filled_amount = (
        requested_amount * filled_contracts / requested_contracts)
    remaining_amount = requested_amount - filled_amount
    fully_closed = filled_contracts == requested_contracts
    order = {
        'id': order_id, 'clientOrderId': client_order_id,
        'status': 'closed' if fully_closed else 'canceled',
        'amount': requested_contracts, 'filled': filled_contracts,
        'remaining': remaining_contracts,
        'filled_contracts': filled_contracts,
        'filled_amount': filled_amount,
        'info': {'clOrdId': client_order_id},
    }
    if filled_contracts > 0:
        order['average'] = float(average)
    else:
        order['financial_evidence_incomplete'] = True
    progress = {
        'terminal': True, 'absent': False, 'confirmed': True,
        'filled': filled_contracts, 'amount': filled_amount,
        'requested_amount': requested_amount,
        'remaining_amount': remaining_amount,
        'clientOrderId': client_order_id,
        'ids': [order_id] if filled_contracts > 0 else [],
        'read_only_evidence': True,
        'status': 'closed' if fully_closed else 'partial',
        'order': order,
        'order_state': {
            'client_order_id': client_order_id,
            'presence': 'present', 'terminal': True,
            'filled': filled_contracts,
            'remaining': remaining_contracts,
        },
    }
    if filled_contracts > 0:
        progress.update({
            'id': order_id,
            'clientOrderIds': [client_order_id],
            'fully_filled': fully_closed,
            'fully_closed': fully_closed,
        })
    return progress


class CompensationProgressValidationTest(unittest.TestCase):
    CLIENT_ID = 'RProgressContract1'

    def _normalize(self, progress):
        system = TradingSystem.__new__(TradingSystem)
        return system._normalize_compensation_close_progress(
            progress, expected_client_order_id=self.CLIENT_ID,
            expected_contracts=10.0, expected_amount=0.1)

    def test_exact_absent_and_present_terminal_shapes_are_normalized(self):
        cases = (
            (_absent_compensation_progress(self.CLIENT_ID, 0.1),
             'absent', 0.0, None),
            (_terminal_compensation_progress(
                self.CLIENT_ID, 10.0, 0.1, 0.0),
             'present', 0.0, True),
            (_terminal_compensation_progress(
                self.CLIENT_ID, 10.0, 0.1, 6.0),
             'present', 6.0, True),
            (_terminal_compensation_progress(
                self.CLIENT_ID, 10.0, 0.1, 10.0),
             'present', 10.0, True),
        )
        for raw, presence, filled, terminal in cases:
            with self.subTest(presence=presence, filled=filled):
                before = copy.deepcopy(raw)
                normalized = self._normalize(raw)
                self.assertEqual(presence, normalized['presence'])
                self.assertEqual(filled, normalized['filled_contracts'])
                self.assertIs(terminal, normalized['terminal'])
                self.assertEqual(before, raw, 'validator 不得改写输入')

    def test_contradictory_or_noncanonical_progress_is_rejected(self):
        present = _terminal_compensation_progress(
            self.CLIENT_ID, 10.0, 0.1, 6.0)
        absent = _absent_compensation_progress(self.CLIENT_ID, 0.1)
        invalid = []

        def add(name, source, mutate):
            candidate = copy.deepcopy(source)
            mutate(candidate)
            invalid.append((name, candidate))

        add('presence-absent', present,
            lambda value: value.__setitem__('absent', True))
        add('absent-terminal', absent,
            lambda value: value.__setitem__('terminal', True))
        add('absent-extra-order-field', absent,
            lambda value: value.__setitem__('fully_closed', False))
        add('present-terminal', present,
            lambda value: value.__setitem__('terminal', None))
        add('present-order-missing', present,
            lambda value: value.__setitem__('order', None))
        add('bool-filled', present,
            lambda value: value.__setitem__('filled', True))
        add('nan-filled', present,
            lambda value: value.__setitem__('filled', float('nan')))
        add('negative-filled', present,
            lambda value: value.__setitem__('filled', -1.0))
        add('top-client-id', present,
            lambda value: value.__setitem__('clientOrderId', 'RWrongTop'))
        add('state-client-id', present,
            lambda value: value['order_state'].__setitem__(
                'client_order_id', 'RWrongState'))
        add('order-client-id', present,
            lambda value: value['order'].__setitem__(
                'clientOrderId', 'RWrongOrder'))
        add('state-filled', present,
            lambda value: value['order_state'].__setitem__('filled', 5.0))
        add('filled-amount', present,
            lambda value: value['order'].__setitem__(
                'filled_amount', 0.05))
        add('multiple-order-ids', present,
            lambda value: value['order'].__setitem__(
                'ids', ['compensation-order', 'another-order']))
        add('top-order-id', present,
            lambda value: value.__setitem__('id', 'another-order'))
        add('top-client-order-ids', present,
            lambda value: value.__setitem__(
                'clientOrderIds', ['RWrongTop']))
        add('fully-filled-not-bool', present,
            lambda value: value.__setitem__('fully_filled', 0))
        add('fully-closed-missing', present,
            lambda value: value.pop('fully_closed'))

        for name, raw in invalid:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self._normalize(raw)


class SignalExecutionStateTest(unittest.TestCase):
    def test_persisted_close_intent_rejects_bool_planned_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            state = TradeState(path)
            state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'stop-1',
                strategy='ma_cross')
            state.prepare_close_intent(
                'BTCUSDT', 'CloseIntent123', 'schema regression')
            invalid = state._snapshot_locked()
            invalid['open_positions']['BTCUSDT'][
                'close_intent']['planned_position_size'] = True

            with self.assertRaisesRegex(ValueError, 'planned_position_size'):
                TradeState.validate_state(invalid)

    def test_close_intent_survives_restart_and_is_consumed_with_full_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            state = TradeState(path)
            state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'stop-1',
                strategy='ma_cross')
            first = state.prepare_close_intent(
                'BTCUSDT', 'CloseIntent123', '信号平仓')
            retry = TradeState(path).prepare_close_intent(
                'BTCUSDT', 'DifferentIgnored', 'API 重试')
            self.assertEqual(first['client_order_id'], retry['client_order_id'])

            reloaded = TradeState(path)
            with self.assertRaises(TradeStatePersistenceError):
                reloaded.close_position('BTCUSDT', 105.0)
            closed = reloaded.close_position(
                'BTCUSDT', 105.0,
                close_intent_client_id='CloseIntent123')

            self.assertIsNone(reloaded.get_open_position('BTCUSDT'))
            self.assertEqual(
                'CloseIntent123', closed['last_close_client_order_id'])
            self.assertIsNone(TradeState(path).get_close_intent('BTCUSDT'))

    def test_partial_close_atomically_consumes_close_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            state = TradeState(path)
            state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'stop-1',
                strategy='ma_cross')
            state.prepare_close_intent(
                'BTCUSDT', 'ClosePartial123', '手动平仓')

            state.apply_partial_close(
                'BTCUSDT', 0.4, 105.0, remaining_size=0.6,
                new_stop_order_id='stop-2', stop_order_size=0.6,
                close_intent_client_id='ClosePartial123')

            reloaded = TradeState(path)
            position = reloaded.get_open_position('BTCUSDT')
            self.assertAlmostEqual(0.6, position['position_size'])
            self.assertIsNone(reloaded.get_close_intent('BTCUSDT'))
            self.assertEqual(
                'ClosePartial123', position['last_close_client_order_id'])



class OpenIntentStateTest(unittest.TestCase):
    def test_persisted_open_intent_payload_rejects_bool_prices(self):
        for field in ('entry_price', 'stop_loss_price'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, 'trade_state.json')
                state = TradeState(path)
                state.prepare_open_intent(
                    'BTCUSDT', 'ma_cross', 'long', 'IABC123',
                    {'side': 'long', 'entry_price': 100,
                     'stop_loss_price': 90}, planned_position_size=1.0)
                invalid = state._snapshot_locked()
                invalid['open_intents']['BTCUSDT']['payload'][field] = True

                with self.assertRaisesRegex(ValueError, field):
                    TradeState.validate_state(invalid)

                with open(path, 'w', encoding='utf-8') as stream:
                    json.dump(invalid, stream)
                os.chmod(path, 0o600)
                with self.assertRaises(TradeStatePersistenceError):
                    TradeState(path)

    def test_round_trip_finalizer_rejects_bool_and_keeps_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IABC123',
                {'side': 'long', 'entry_price': 100,
                 'stop_loss_price': 90}, planned_position_size=1.0)
            valid = {
                'entry_price': 100.0,
                'exit_price': 99.0,
                'position_size': 1.0,
            }
            for field in tuple(valid):
                malformed = dict(valid)
                malformed[field] = True
                with self.subTest(field=field), self.assertRaises(ValueError):
                    state.finalize_open_intent_round_trip(
                        'BTCUSDT', 'IABC123', **malformed)
                self.assertIsNotNone(state.get_open_intent('BTCUSDT'))
                self.assertEqual([], state.get_closed_trades())

    def test_recovery_writers_reject_bool_money_fields_and_keep_intent(self):
        cases = []
        partial = {
            'entry_price': 100.0, 'original_size': 1.0,
            'remaining_size': 0.4, 'stop_loss_price': 90.0,
            'partial_exit_price': 99.0, 'stop_order_size': 0.4,
        }
        for field in tuple(partial):
            malformed = dict(partial)
            malformed[field] = True
            cases.append(('partial_' + field, 'partial', malformed))
        untracked = {
            'entry_price': 100.0, 'position_size': 1.0,
            'stop_loss_price': 90.0, 'stop_order_size': 1.0,
        }
        for field in tuple(untracked):
            malformed = dict(untracked)
            malformed[field] = True
            cases.append(('untracked_' + field, 'untracked', malformed))

        for name, writer, values in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                state = TradeState(os.path.join(tmp, 'trade_state.json'))
                state.prepare_open_intent(
                    'BTCUSDT', 'ma_cross', 'long', 'IABC123',
                    {'side': 'long', 'entry_price': 100,
                     'stop_loss_price': 90}, planned_position_size=1.0)

                with self.assertRaises(ValueError):
                    if writer == 'partial':
                        state.add_open_after_partial_rollback(
                            'BTCUSDT', 'long',
                            values['entry_price'], values['original_size'],
                            values['remaining_size'], values['stop_loss_price'],
                            values['partial_exit_price'],
                            stop_order_size=values['stop_order_size'],
                            strategy='ma_cross',
                            open_intent_client_id='IABC123')
                    else:
                        state.add_untracked_open_position(
                            'BTCUSDT', 'long', values['entry_price'],
                            values['position_size'], values['stop_loss_price'],
                            stop_order_size=values['stop_order_size'],
                            strategy='ma_cross',
                            open_intent_client_id='IABC123')

                self.assertIsNone(state.get_open_position('BTCUSDT'))
                self.assertIsNotNone(state.get_open_intent('BTCUSDT'))

    def test_intent_and_planned_amount_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            state = TradeState(path)
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IABC123',
                {'side': 'long', 'entry_price': 100,
                 'stop_loss_price': 90}, planned_position_size=1.25)

            pending = TradeState(path).get_open_intent('BTCUSDT')

            self.assertEqual('IABC123', pending['client_order_id'])
            self.assertEqual(1.25, pending['planned_position_size'])

    def test_position_and_intent_clear_commit_in_one_transaction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_state.json')
            state = TradeState(path)
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IABC123',
                {'side': 'long', 'entry_price': 100,
                 'stop_loss_price': 90}, planned_position_size=1.0)

            state.add_open_position(
                'BTCUSDT', 'long', 100, 1, 90, 'stop-1',
                strategy='ma_cross', open_intent_client_id='IABC123')

            reloaded = TradeState(path)
            self.assertIsNotNone(reloaded.get_open_position('BTCUSDT'))
            self.assertIsNone(reloaded.get_open_intent('BTCUSDT'))

    def test_failed_atomic_position_save_keeps_intent_and_no_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IABC123',
                {'side': 'long', 'entry_price': 100,
                 'stop_loss_price': 90}, planned_position_size=1.0)
            with patch('trade_state.atomic_write_json', return_value=False):
                with self.assertRaises(TradeStatePersistenceError):
                    state.add_open_position(
                        'BTCUSDT', 'long', 100, 1, 90, 'stop-1',
                        strategy='ma_cross',
                        open_intent_client_id='IABC123')

            self.assertIsNone(state.get_open_position('BTCUSDT'))
            self.assertIsNotNone(state.get_open_intent('BTCUSDT'))

    def test_same_client_id_cannot_change_open_intent_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            payload = {
                'side': 'long', 'entry_price': 100,
                'stop_loss_price': 90,
            }
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IABC123', payload,
                planned_position_size=1.0)

            with self.assertRaises(TradeStatePersistenceError):
                state.prepare_open_intent(
                    'BTCUSDT', 'ma_cross', 'short', 'IABC123',
                    {**payload, 'side': 'short'},
                    planned_position_size=1.0)
            with self.assertRaises(TradeStatePersistenceError):
                state.prepare_open_intent(
                    'BTCUSDT', 'ma_cross', 'long', 'IABC123',
                    {**payload, 'stop_loss_price': 89},
                    planned_position_size=1.0)

            pending = state.get_open_intent('BTCUSDT')
            self.assertEqual('long', pending['side'])
            self.assertEqual(payload, pending['payload'])

    def test_position_commit_must_match_pending_open_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IABC123',
                {'side': 'long', 'entry_price': 100,
                 'stop_loss_price': 90},
                planned_position_size=1.0)

            with self.assertRaises(TradeStatePersistenceError):
                state.add_open_position(
                    'BTCUSDT', 'short', 100, 1, 110, 'stop-1',
                    strategy='ma_cross',
                    open_intent_client_id='IABC123')

            self.assertIsNone(state.get_open_position('BTCUSDT'))
            self.assertIsNotNone(state.get_open_intent('BTCUSDT'))

    def test_normal_position_writer_still_rejects_overplanned_fill(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IABC123',
                {'side': 'long', 'entry_price': 100,
                 'stop_loss_price': 90},
                planned_position_size=1.0)

            with self.assertRaises(TradeStatePersistenceError):
                state.add_open_position(
                    'BTCUSDT', 'long', 100, 1.2, 90, 'stop-1',
                    strategy='ma_cross',
                    open_intent_client_id='IABC123')

            self.assertIsNone(state.get_open_position('BTCUSDT'))
            self.assertIsNotNone(state.get_open_intent('BTCUSDT'))

    def test_overfill_recovery_requires_exact_original_request_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IABC123',
                {'side': 'long', 'entry_price': 100,
                 'stop_loss_price': 90},
                planned_position_size=1.0)

            with self.assertRaises(TradeStatePersistenceError):
                state.add_untracked_open_position(
                    'BTCUSDT', 'long', 100, 1.2, 90,
                    strategy='ma_cross',
                    open_intent_client_id='IABC123',
                    requested_position_size=0.9)

            self.assertIsNone(state.get_open_position('BTCUSDT'))
            self.assertIsNotNone(state.get_open_intent('BTCUSDT'))

    def test_recovery_position_writers_also_bind_pending_intent_semantics(self):
        writers = (
            ('partial', lambda state, symbol, client_id:
                state.add_open_after_partial_rollback(
                    symbol, 'short', 100, 1.0, 0.4, 90, 99,
                    strategy='ma_cross',
                    open_intent_client_id=client_id)),
            ('untracked', lambda state, symbol, client_id:
                state.add_untracked_open_position(
                    symbol, 'short', 100, 1.0, 90,
                    strategy='ma_cross',
                    open_intent_client_id=client_id)),
        )
        for name, writer in writers:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                state = TradeState(os.path.join(tmp, 'trade_state.json'))
                state.prepare_open_intent(
                    'BTCUSDT', 'ma_cross', 'long', 'IABC123',
                    {'side': 'long', 'entry_price': 100,
                     'stop_loss_price': 90},
                    planned_position_size=1.0)

                with self.assertRaises(TradeStatePersistenceError):
                    writer(state, 'BTCUSDT', 'IABC123')

                self.assertIsNone(state.get_open_position('BTCUSDT'))
                self.assertIsNotNone(state.get_open_intent('BTCUSDT'))

    def test_single_strategy_writer_defaults_to_ma_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))

            position = state.add_open_position(
                'BTCUSDT', 'long', 100, 1, 90, 'stop-1')

            self.assertEqual('ma_cross', position['strategy'])

    def test_open_position_and_open_intent_are_mutually_exclusive(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            state.add_open_position(
                'BTCUSDT', 'long', 100, 1, 90, 'stop-1',
                strategy='ma_cross')

            with self.assertRaises(TradeStatePersistenceError):
                state.prepare_open_intent(
                    'BTCUSDT', 'ma_cross', 'long', 'IABC123',
                    {'side': 'long', 'entry_price': 100,
                     'stop_loss_price': 90},
                    planned_position_size=1.0)

            invalid = state._snapshot_locked()
            invalid['open_intents']['BTCUSDT'] = {
                'strategy': 'ma_cross', 'side': 'long',
                'client_order_id': 'IABC123', 'status': 'pending',
                'payload': {'side': 'long', 'entry_price': 100,
                            'stop_loss_price': 90},
                'planned_position_size': 1.0,
                'created_at': datetime.now().isoformat(),
                'updated_at': datetime.now().isoformat(),
            }
            with self.assertRaisesRegex(
                    ValueError, 'position 与 open_intent 共存'):
                TradeState.validate_state(invalid)


class PartialOpenCrashRecoveryTest(unittest.TestCase):
    def test_existing_stop_post_anchor_wins_over_older_intent_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            intent = state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IAnchor123',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=1.0)
            state.mark_open_intent_unresolved_execution(
                'BTCUSDT', 'IAnchor123', 'open_compensation', 1.0,
                compensation_client_order_id='RAnchor123')
            state.mark_stop_residue('BTCUSDT')
            post_anchor = state.get_stop_residues()['BTCUSDT']

            state.add_untracked_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0,
                strategy='ma_cross', stop_residue_possible=True,
                stop_residue_marked_at=intent['created_at'],
                open_intent_client_id='IAnchor123',
                preserve_open_intent=True)

            self.assertEqual(
                post_anchor, state.get_stop_residues()['BTCUSDT'])

    def test_terminal_partial_open_builds_read_only_provisional_by_actual_fill(self):
        cases = (
            ('absent', 0.0, 5.0),
            ('present_zero', 0.0, 5.0),
            ('present_partial', 2.0, 3.0),
        )
        for progress_kind, compensation_filled, fresh_contracts in cases:
            with self.subTest(
                    progress_kind=progress_kind,
                    compensation_filled=compensation_filled), \
                    tempfile.TemporaryDirectory() as tmp:
                system = TradingSystem.__new__(TradingSystem)
                system.trade_state = TradeState(
                    os.path.join(tmp, 'trade_state.json'))
                intent = system.trade_state.prepare_open_intent(
                    'BTCUSDT', 'ma_cross', 'long', 'IPartialCrash123',
                    {'side': 'long', 'entry_price': 100.0,
                     'stop_loss_price': 90.0},
                    planned_position_size=10.0)
                old_anchor = (datetime.now() - timedelta(minutes=10)).isoformat()
                with system.trade_state.lock:
                    system.trade_state.state['open_intents']['BTCUSDT'][
                        'created_at'] = old_anchor
                    system.trade_state.save_state()
                intent = system.trade_state.get_open_intent('BTCUSDT')
                open_post = Mock()
                close_post = Mock()
                compensation_id = 'RPartialCrash123'
                progress = (
                    _absent_compensation_progress(compensation_id, 5.0)
                    if progress_kind == 'absent' else
                    _terminal_compensation_progress(
                        compensation_id, 5.0, 5.0,
                        compensation_filled,
                        order_id='compensation-partial'))
                system.exchange_api = SimpleNamespace(
                    to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT',
                    _coin_to_contracts=lambda _symbol, amount: float(amount),
                    _contracts_to_coins=lambda _symbol, amount: float(amount),
                    find_existing_open_order=Mock(return_value={
                        'id': 'open-partial', 'status': 'canceled',
                        'amount': 10.0, 'filled': 5.0, 'remaining': 5.0,
                        'average': 100.0,
                    }),
                    find_compensation_close_progress=Mock(
                        return_value=progress),
                    compensation_client_order_id=(
                        lambda value: f'R{value[1:]}'),
                    get_position=Mock(return_value={
                        'side': 'long', 'contracts': fresh_contracts}),
                    open_position=open_post,
                    close_position=close_post,
                )
                system._quarantine_position_mismatch = Mock()
                system._protect_unresolved_lifecycle_position = Mock(
                    return_value=False)

                recovered = system._resume_open_intent_position(
                    'BTCUSDT', intent)

                self.assertFalse(recovered)
                open_post.assert_not_called()
                close_post.assert_not_called()
                system.exchange_api.find_compensation_close_progress.assert_called_once_with(
                    'BTC/USDT:USDT', 'long', 5.0, 'IPartialCrash123')
                position = system.trade_state.get_open_position('BTCUSDT')
                self.assertEqual(fresh_contracts, position['position_size'])
                self.assertTrue(position['recovered_unresolved_open'])
                self.assertFalse(position['execution_recovery_finalized'])
                self.assertEqual([], position.get('partial_closes', []))
                self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))
                self.assertEqual(
                    old_anchor,
                    system.trade_state.get_stop_residues()['BTCUSDT'])
                unresolved = system.trade_state.get_open_intent(
                    'BTCUSDT')['unresolved_execution']
                self.assertEqual(5.0, unresolved['expected_position_size'])
                system._quarantine_position_mismatch.assert_not_called()
                system._protect_unresolved_lifecycle_position.assert_called_once()

    def test_malformed_progress_cannot_build_provisional_or_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(
                os.path.join(tmp, 'trade_state.json'))
            intent = system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IPartialCrashBad1',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=10.0)
            compensation_id = 'RPartialCrashBad1'
            malformed = _absent_compensation_progress(
                compensation_id, 5.0)
            malformed['terminal'] = True
            open_post = Mock()
            close_post = Mock()
            get_position = Mock(return_value={
                'side': 'long', 'contracts': 5.0})
            system.exchange_api = SimpleNamespace(
                to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT',
                _coin_to_contracts=lambda _symbol, amount: float(amount),
                _contracts_to_coins=lambda _symbol, amount: float(amount),
                find_existing_open_order=Mock(return_value={
                    'id': 'open-partial', 'status': 'canceled',
                    'amount': 10.0, 'filled': 5.0, 'remaining': 5.0,
                    'average': 100.0,
                }),
                find_compensation_close_progress=Mock(
                    return_value=malformed),
                compensation_client_order_id=Mock(
                    return_value=compensation_id),
                get_position=get_position,
                open_position=open_post, close_position=close_post,
            )
            system._quarantine_position_mismatch = Mock(return_value=True)

            self.assertFalse(system._resume_open_intent_position(
                'BTCUSDT', intent))

            self.assertIsNone(
                system.trade_state.get_open_position('BTCUSDT'))
            self.assertIsNone(
                system.trade_state.get_open_intent(
                    'BTCUSDT').get('unresolved_execution'))
            get_position.assert_not_called()
            open_post.assert_not_called()
            close_post.assert_not_called()
            system._quarantine_position_mismatch.assert_called_once()


class CompensationProgressOrchestrationTest(unittest.TestCase):
    OPEN_ID = 'IReconcileProgress1'
    COMPENSATION_ID = 'RReconcileProgress1'

    def _reconcile_system(self, tmp, progress):
        system = TradingSystem.__new__(TradingSystem)
        system.trade_state = TradeState(
            os.path.join(tmp, 'trade_state.json'))
        system.trade_state.prepare_open_intent(
            'BTCUSDT', 'ma_cross', 'long', self.OPEN_ID,
            {'side': 'long', 'entry_price': 100.0,
             'stop_loss_price': 90.0},
            planned_position_size=1.0)
        system.trade_state.mark_open_intent_unresolved_execution(
            'BTCUSDT', self.OPEN_ID, 'open_compensation', 1.0,
            compensation_client_order_id=self.COMPENSATION_ID)
        system.trade_state.add_untracked_open_position(
            symbol='BTCUSDT', side='long', entry_price=100.0,
            position_size=0.4, stop_loss_price=90.0,
            stop_order_id='stop-provisional', stop_order_size=0.4,
            strategy='ma_cross', open_intent_client_id=self.OPEN_ID,
            requested_position_size=1.0, preserve_open_intent=True)
        open_post = Mock()
        close_post = Mock()
        get_position = Mock(return_value={
            'side': 'long', 'contracts': 0.4})
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda symbol: symbol,
            _get_contract_size=lambda _symbol: 1.0,
            _coin_to_contracts=lambda _symbol, amount: float(amount),
            find_existing_open_order=Mock(return_value={
                'id': 'open-reconciled', 'status': 'closed',
                'amount': 1.0, 'filled': 1.0, 'remaining': 0.0,
                'average': 100.0,
            }),
            compensation_client_order_id=Mock(
                return_value=self.COMPENSATION_ID),
            find_compensation_close_progress=Mock(
                return_value=progress),
            get_position=get_position,
            open_position=open_post, close_position=close_post,
        )
        system._quarantine_position_mismatch = Mock(return_value=True)
        return system, get_position, open_post, close_post

    def test_reconcile_accepts_one_terminal_partial_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = _terminal_compensation_progress(
                self.COMPENSATION_ID, 1.0, 1.0, 0.6,
                order_id='compensation-partial', average=99.0)
            system, _get_position, open_post, close_post = (
                self._reconcile_system(tmp, progress))
            intent = system.trade_state.get_open_intent('BTCUSDT')
            position = system.trade_state.get_open_position('BTCUSDT')

            self.assertTrue(system._reconcile_position_open_intent(
                'BTCUSDT', intent, position))

            self.assertIsNone(
                system.trade_state.get_open_intent('BTCUSDT'))
            reconciled = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual(0.4, reconciled['position_size'])
            self.assertTrue(reconciled['execution_recovery_finalized'])
            self.assertEqual(1, len(reconciled['partial_closes']))
            self.assertEqual(
                ['compensation-partial'],
                reconciled['partial_closes'][0]['exit_order_ids'])
            open_post.assert_not_called()
            close_post.assert_not_called()

    def test_reconcile_rejects_identity_conflict_without_consuming_blocker(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = _terminal_compensation_progress(
                self.COMPENSATION_ID, 1.0, 1.0, 0.6)
            progress['order_state']['client_order_id'] = 'RWrongState'
            system, get_position, open_post, close_post = (
                self._reconcile_system(tmp, progress))
            intent = system.trade_state.get_open_intent('BTCUSDT')
            position = system.trade_state.get_open_position('BTCUSDT')

            self.assertFalse(system._reconcile_position_open_intent(
                'BTCUSDT', intent, position))

            retained = system.trade_state.get_open_intent('BTCUSDT')
            self.assertIsNotNone(retained['unresolved_execution'])
            self.assertFalse(system.trade_state.get_open_position(
                'BTCUSDT')['execution_recovery_finalized'])
            self.assertEqual([], system.trade_state.get_open_position(
                'BTCUSDT').get('partial_closes', []))
            get_position.assert_not_called()
            open_post.assert_not_called()
            close_post.assert_not_called()
            system._quarantine_position_mismatch.assert_called_once()

    def test_reconcile_keeps_blocker_when_structure_is_valid_but_finance_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = _terminal_compensation_progress(
                self.COMPENSATION_ID, 1.0, 1.0, 0.6,
                order_id='compensation-no-average')
            progress['order'].pop('average')
            progress['order']['financial_evidence_incomplete'] = True
            system, get_position, open_post, close_post = (
                self._reconcile_system(tmp, progress))
            intent = system.trade_state.get_open_intent('BTCUSDT')
            position = system.trade_state.get_open_position('BTCUSDT')

            self.assertFalse(system._reconcile_position_open_intent(
                'BTCUSDT', intent, position))

            self.assertIsNotNone(
                system.trade_state.get_open_intent(
                    'BTCUSDT')['unresolved_execution'])
            retained = system.trade_state.get_open_position('BTCUSDT')
            self.assertFalse(retained['execution_recovery_finalized'])
            self.assertEqual([], retained.get('partial_closes', []))
            get_position.assert_called_once()
            open_post.assert_not_called()
            close_post.assert_not_called()
            system._quarantine_position_mismatch.assert_called_once()

    def test_reconcile_rejects_nan_contract_conversion_before_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = _terminal_compensation_progress(
                self.COMPENSATION_ID, 1.0, 1.0, 0.6)
            system, get_position, open_post, close_post = (
                self._reconcile_system(tmp, progress))
            system.exchange_api._coin_to_contracts = (
                lambda _symbol, _amount: float('nan'))
            intent = system.trade_state.get_open_intent('BTCUSDT')
            position = system.trade_state.get_open_position('BTCUSDT')

            self.assertFalse(system._reconcile_position_open_intent(
                'BTCUSDT', intent, position))

            self.assertIsNotNone(
                system.trade_state.get_open_intent(
                    'BTCUSDT')['unresolved_execution'])
            self.assertFalse(system.trade_state.get_open_position(
                'BTCUSDT')['execution_recovery_finalized'])
            system.exchange_api.find_compensation_close_progress.assert_not_called()
            get_position.assert_not_called()
            open_post.assert_not_called()
            close_post.assert_not_called()
            system._quarantine_position_mismatch.assert_called_once()

    def _attribution_system(self, tmp, progress):
        system = TradingSystem.__new__(TradingSystem)
        system.trade_state = TradeState(
            os.path.join(tmp, 'trade_state.json'))
        system.trade_state.prepare_open_intent(
            'BTCUSDT', 'ma_cross', 'long', self.OPEN_ID,
            {'side': 'long', 'entry_price': 100.0,
             'stop_loss_price': 90.0},
            planned_position_size=1.0)
        system.trade_state.mark_open_intent_unresolved_execution(
            'BTCUSDT', self.OPEN_ID, 'open_attribution', 1.0,
            protective_stop_order_id='stop-attribution',
            protective_stop_order_size=1.0)
        confirm_stop = Mock(return_value=True)
        open_post = Mock()
        close_post = Mock()
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda symbol: symbol,
            _get_contract_size=lambda _symbol: 1.0,
            get_position=Mock(side_effect=[None, None]),
            compensation_client_order_id=Mock(
                return_value=self.COMPENSATION_ID),
            find_compensation_close_progress=Mock(
                return_value=progress),
            confirm_stop_execution=confirm_stop,
            open_position=open_post, close_position=close_post,
        )
        system._quarantine_position_mismatch = Mock(return_value=True)
        system._clear_position_quarantine_after_reconcile = Mock()
        return system, confirm_stop, open_post, close_post

    def test_flat_attribution_accepts_absent_and_terminal_zero_fill_only(self):
        progress_cases = (
            _absent_compensation_progress(self.COMPENSATION_ID, 1.0),
            _terminal_compensation_progress(
                self.COMPENSATION_ID, 1.0, 1.0, 0.0,
                order_id='compensation-zero'),
        )
        for progress in progress_cases:
            with self.subTest(presence=progress['order_state']['presence']), \
                    tempfile.TemporaryDirectory() as tmp:
                system, confirm_stop, open_post, close_post = (
                    self._attribution_system(tmp, progress))
                intent = system.trade_state.get_open_intent('BTCUSDT')

                self.assertFalse(system._finalize_flat_filled_open_intent(
                    'BTCUSDT', intent,
                    {'id': 'open-proof', 'average': 100.0}, 1.0))

                self.assertIsNotNone(
                    system.trade_state.get_open_intent(
                        'BTCUSDT')['unresolved_execution'])
                confirm_stop.assert_called_once()
                open_post.assert_not_called()
                close_post.assert_not_called()
                system._quarantine_position_mismatch.assert_called_once()

    def test_flat_attribution_rejects_malformed_progress_before_stop_adjudication(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = _terminal_compensation_progress(
                self.COMPENSATION_ID, 1.0, 1.0, 0.0,
                order_id='compensation-zero')
            progress['order'] = None
            system, confirm_stop, open_post, close_post = (
                self._attribution_system(tmp, progress))
            intent = system.trade_state.get_open_intent('BTCUSDT')

            with self.assertRaises(ValueError):
                system._finalize_flat_filled_open_intent(
                    'BTCUSDT', intent,
                    {'id': 'open-proof', 'average': 100.0}, 1.0)

            self.assertIsNotNone(
                system.trade_state.get_open_intent(
                    'BTCUSDT')['unresolved_execution'])
            confirm_stop.assert_not_called()
            open_post.assert_not_called()
            close_post.assert_not_called()
            system._quarantine_position_mismatch.assert_not_called()

    def test_flat_attribution_rejects_valid_partial_before_stop_adjudication(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = _terminal_compensation_progress(
                self.COMPENSATION_ID, 1.0, 1.0, 0.2,
                order_id='compensation-partial')
            system, confirm_stop, open_post, close_post = (
                self._attribution_system(tmp, progress))
            intent = system.trade_state.get_open_intent('BTCUSDT')

            with self.assertRaisesRegex(RuntimeError, '非零成交'):
                system._finalize_flat_filled_open_intent(
                    'BTCUSDT', intent,
                    {'id': 'open-proof', 'average': 100.0}, 1.0)

            self.assertIsNotNone(
                system.trade_state.get_open_intent(
                    'BTCUSDT')['unresolved_execution'])
            confirm_stop.assert_not_called()
            open_post.assert_not_called()
            close_post.assert_not_called()

    def test_flat_attribution_rejects_expired_absent_before_stop_adjudication(self):
        with tempfile.TemporaryDirectory() as tmp:
            progress = _absent_compensation_progress(
                self.COMPENSATION_ID, 1.0)
            system, confirm_stop, open_post, close_post = (
                self._attribution_system(tmp, progress))
            intent = system.trade_state.get_open_intent('BTCUSDT')
            intent['unresolved_execution']['created_at'] = (
                datetime.now() - timedelta(minutes=91)).isoformat()

            with self.assertRaisesRegex(RuntimeError, '不可证明零成交'):
                system._finalize_flat_filled_open_intent(
                    'BTCUSDT', intent,
                    {'id': 'open-proof', 'average': 100.0}, 1.0)

            self.assertIsNotNone(
                system.trade_state.get_open_intent(
                    'BTCUSDT')['unresolved_execution'])
            confirm_stop.assert_not_called()
            open_post.assert_not_called()
            close_post.assert_not_called()


class GenericOpenIntentIntegrationTest(unittest.TestCase):
    def _system(self, tmp, *, exchange_position=None):
        system = TradingSystem.__new__(TradingSystem)
        system.base_dir = tmp
        system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
        seen_client_ids = []
        seen_require_existing = []
        exchange_state = {'position': exchange_position}

        def open_position(
                _symbol, _side, amount, client_order_id=None, *,
                require_existing=False):
            seen_client_ids.append(client_order_id)
            seen_require_existing.append(require_existing)
            exchange_state['position'] = {
                'side': _side, 'contracts': float(amount)}
            return {
                'id': 'open-1', 'average': 100.0, 'amount': amount,
                'confirmed': True, 'fully_filled': True,
            }

        def find_existing_open_order(
                _symbol, _side, amount, _client_order_id,
                wait_for_visibility=False):
            self.assertTrue(wait_for_visibility)
            return {
                'id': 'open-recovered', 'status': 'closed',
                'amount': float(amount), 'filled': float(amount),
                'remaining': 0.0, 'average': 100.0,
            }

        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda symbol: symbol,
            get_last_price=lambda _symbol: 100.0,
            get_balance=lambda: {'total': {'USDT': 1000.0}},
            round_quantity=lambda _symbol, amount: amount,
            get_quantity_precision=lambda _symbol: 3,
            open_position=open_position,
            create_stop_loss_order=lambda *args, **kwargs: {'id': 'stop-1'},
            get_position=lambda _symbol: exchange_state['position'],
            _get_contract_size=lambda _symbol: 1.0,
            _coin_to_contracts=lambda _symbol, amount: float(amount),
            _contracts_to_coins=lambda _symbol, contracts: float(contracts),
            find_existing_open_order=Mock(
                side_effect=find_existing_open_order),
            find_compensation_close_progress=Mock(side_effect=(
                lambda _symbol, _side, amount, open_client_order_id:
                _absent_compensation_progress(
                    f'R{open_client_order_id[1:]}', amount))),
            compensation_client_order_id=lambda value: f'R{value[1:]}',
        )
        system.config = {
            'strategy': {'default_risk_per_trade': 0.01},
            'trading': {'symbols': [{
                'name': 'BTCUSDT', 'enabled': True,
                'risk_per_trade': 0.01,
            }]},
        }
        system.risk_manager = SimpleNamespace(
            account_equity=1000.0, risk_per_trade=0.01,
            calculate_position_size=lambda *_args: 1.0)
        system.notifier = SimpleNamespace(
            notify_error=Mock(), send_message=Mock())
        system._pending_trade_open_notifications = []
        system._stop_anomalies = {}
        system.stop_loss_dates = {}
        system._seen_require_existing = seen_require_existing
        system._exchange_position_state = exchange_state
        return system, seen_client_ids

    def test_ma_open_persists_intent_before_post_and_clears_with_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(tmp)

            outcome = system._execute_open(
                'BTCUSDT', 'long', 100.0, 90.0,
                {'name': 'BTCUSDT',
                 'risk_per_trade': 0.01})

            self.assertEqual('opened', outcome['status'])
            self.assertEqual(1, len(seen_client_ids))
            self.assertTrue(seen_client_ids[0].startswith('I'))
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            self.assertIsNotNone(system.trade_state.get_open_position('BTCUSDT'))

    def test_maintenance_sentinel_blocks_all_open_entry_shapes_before_exchange(self):
        """普通与即时开仓最终都必须服从同一个中央禁开仓哨兵。"""
        entry_shapes = (
            ('ordinary', {}),
            ('instant', {'buffer_notification': False}),
        )
        for name, kwargs in entry_shapes:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                system, seen_client_ids = self._system(tmp)
                system.base_dir = tmp
                sentinel = os.path.join(tmp, '.maintenance_no_open')
                with open(sentinel, 'w', encoding='utf-8') as handle:
                    handle.write('deployment maintenance\n')

                outcome = system._execute_open(
                    'BTCUSDT', 'long', 100.0, 90.0,
                    {'name': 'BTCUSDT', 'risk_per_trade': 0.01},
                    **kwargs)

                self.assertEqual('maintenance_blocked', outcome['status'])
                self.assertEqual([], seen_client_ids)
                self.assertIsNone(
                    system.trade_state.get_open_intent('BTCUSDT'))

    def test_maintenance_sentinel_lstat_error_fails_closed(self):
        """权限/I/O 故障不是“哨兵不存在”，必须在任何开仓查询或 POST 前阻断。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(tmp)
            system.base_dir = tmp

            with patch('runtime_guard.os.lstat',
                       side_effect=PermissionError('sentinel unreadable')):
                outcome = system._execute_open(
                    'BTCUSDT', 'long', 100.0, 90.0,
                    {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

            self.assertEqual('maintenance_blocked', outcome['status'])
            self.assertEqual([], seen_client_ids)
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))

    def test_explicit_external_maintenance_sentinel_takes_priority(self):
        """部署 unit 指向外部哨兵时，不得静默退回 base_dir 的 absent 路径。"""
        with (tempfile.TemporaryDirectory() as tmp,
              tempfile.TemporaryDirectory() as external):
            system, seen_client_ids = self._system(tmp)
            sentinel = os.path.join(external, '.maintenance_no_open')
            with open(sentinel, 'w', encoding='utf-8') as handle:
                handle.write('deployment maintenance\n')

            with patch.dict(os.environ, {
                    'TRADING_MAINTENANCE_SENTINEL': sentinel}, clear=False):
                outcome = system._execute_open(
                    'BTCUSDT', 'long', 100.0, 90.0,
                    {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

            self.assertEqual('maintenance_blocked', outcome['status'])
            self.assertEqual([], seen_client_ids)
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))

    def test_relative_explicit_maintenance_sentinel_fails_closed(self):
        """显式相对路径是部署配置错误，不能退回默认 absent 后继续开仓。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(tmp)

            with patch.dict(os.environ, {
                    'TRADING_MAINTENANCE_SENTINEL': 'relative/sentinel'},
                    clear=False):
                outcome = system._execute_open(
                    'BTCUSDT', 'long', 100.0, 90.0,
                    {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

            self.assertEqual('maintenance_blocked', outcome['status'])
            self.assertEqual([], seen_client_ids)
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))

    def test_absent_default_maintenance_sentinel_allows_open(self):
        """未显式配置时只查 base_dir；canonical 路径确实 absent 才允许开仓。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(tmp)

            with patch.dict(os.environ, {}, clear=True):
                outcome = system._execute_open(
                    'BTCUSDT', 'long', 100.0, 90.0,
                    {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

            self.assertEqual('opened', outcome['status'])
            self.assertEqual(1, len(seen_client_ids))
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            self.assertIsNotNone(system.trade_state.get_open_position('BTCUSDT'))

    def test_central_open_boundary_blocks_same_day_stop_for_every_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(tmp)
            system.trade_state.replace_stop_loss_dates({
                'BTCUSDT': date.today().isoformat(),
            })

            outcome = system._execute_open(
                'BTCUSDT', 'long', 100.0, 90.0,
                {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

            self.assertEqual('t1_blocked', outcome['status'])
            self.assertEqual([], seen_client_ids)
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))

    def test_central_open_boundary_blocks_persistence_degraded_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(tmp)
            system.trade_state.mark_runtime_persistence_degraded(
                'test_durability_failure')

            outcome = system._execute_open(
                'BTCUSDT', 'long', 100.0, 90.0,
                {'name': 'BTCUSDT', 'risk_per_trade': 0.01})

            self.assertEqual('state_blocked', outcome['status'])
            self.assertEqual([], seen_client_ids)
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))

    def test_orphan_position_resumes_same_intent_without_recalculating_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(
                tmp, exchange_position={'side': 'long', 'contracts': 1})
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IRECOVER123',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0}, planned_position_size=1.0)
            system.risk_manager.calculate_position_size = Mock(
                side_effect=AssertionError('恢复 open intent 不得重算风险'))

            self.assertEqual(
                {'BTCUSDT'}, system._reconcile_all_open_intents('test'))

            # 崩溃恢复只读找回原始 clOrdId 与确定性补偿 ID，
            # 绝不重放旧开仓请求。先建无财务 provisional，由下一轮
            # lifecycle 用权威终态收口。
            self.assertEqual([], seen_client_ids)
            system.risk_manager.calculate_position_size.assert_not_called()
            intent = system.trade_state.get_open_intent('BTCUSDT')
            self.assertEqual(
                'open_compensation', intent['unresolved_execution']['kind'])
            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual('long', position['side'])
            self.assertTrue(position['recovered_unresolved_open'])
            self.assertFalse(position['execution_recovery_finalized'])
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))
            self.assertEqual([], system._seen_require_existing)

    def test_orphan_recovery_never_replays_open_and_keeps_failed_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _seen_client_ids = self._system(
                tmp, exchange_position={'side': 'long', 'contracts': 1})
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IRECOVERDISAPPEAR',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0}, planned_position_size=1.0)
            # 原始 clOrdId 连续查无时只隔离，不得把旧 intent
            # 重放成一笔新的 open POST。
            system.exchange_api.find_existing_open_order = Mock(
                return_value=None)
            system.exchange_api.open_position = Mock()

            self.assertEqual(
                {'BTCUSDT'}, system._reconcile_all_open_intents('test'))

            system.exchange_api.open_position.assert_not_called()
            system.exchange_api.find_existing_open_order.assert_called_once_with(
                'BTCUSDT', 'long', 1.0, 'IRECOVERDISAPPEAR',
                wait_for_visibility=True)
            self.assertIsNotNone(
                system.trade_state.get_open_intent('BTCUSDT'))
            self.assertTrue(
                system.trade_state.is_position_quarantined('BTCUSDT'))

    def test_pre_post_crash_intent_without_amount_is_consumed_not_guessed(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(tmp, exchange_position=None)
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IUNSUBMITTED123',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0})

            self.assertEqual(set(), system._reconcile_all_open_intents('test'))

            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            self.assertEqual([], seen_client_ids)

    def test_flat_unsubmitted_intent_is_consumed_when_symbol_retired(self):
        """删除/禁用发生在 intent 落盘后时，不得把恢复事务变成一笔新开仓。"""
        retired_configs = (
            [],
            [{'name': 'BTCUSDT', 'enabled': False,
              'risk_per_trade': 0.01}],
        )
        for symbols in retired_configs:
            with self.subTest(symbols=symbols), tempfile.TemporaryDirectory() as tmp:
                system, seen_client_ids = self._system(
                    tmp, exchange_position=None)
                system.exchange_api.find_existing_open_order = Mock(
                    return_value=None)
                system.trade_state.prepare_open_intent(
                    'BTCUSDT', 'ma_cross', 'long', 'IRETIRED123',
                    {'side': 'long', 'entry_price': 100.0,
                     'stop_loss_price': 90.0},
                    planned_position_size=1.0)
                system.config['trading']['symbols'] = symbols

                self.assertEqual(
                    set(), system._reconcile_all_open_intents('test'))

                self.assertEqual([], seen_client_ids)
                self.assertIsNone(
                    system.trade_state.get_open_intent('BTCUSDT'))
                self.assertIsNone(
                    system.trade_state.get_open_position('BTCUSDT'))

    def test_flat_absent_open_intent_is_closed_without_replaying_old_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(
                tmp, exchange_position=None)
            system.exchange_api.find_existing_open_order = Mock(
                return_value=None)
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IABSENTACTIVE123',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=1.0)

            self.assertEqual(
                set(), system._reconcile_all_open_intents('test'))

            self.assertEqual([], seen_client_ids)
            self.assertIsNone(
                system.trade_state.get_open_intent('BTCUSDT'))
            system.exchange_api.find_existing_open_order.assert_called_once_with(
                'BTCUSDT', 'long', 1.0, 'IABSENTACTIVE123',
                wait_for_visibility=True)

    def test_terminal_zero_fill_rechecks_position_before_consuming_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _seen_client_ids = self._system(tmp)
            system.exchange_api.get_position = Mock(side_effect=[
                None, {'side': 'long', 'contracts': 1.0}])
            system.exchange_api.find_existing_open_order = Mock(return_value={
                'id': 'open-zero', 'status': 'canceled',
                'amount': 1.0, 'filled': 0.0, 'remaining': 1.0,
            })
            system._resume_open_intent_position = Mock(return_value=False)
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IZERORACE123',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0}, planned_position_size=1.0)

            self.assertEqual(
                {'BTCUSDT'}, system._reconcile_all_open_intents('test'))

            self.assertEqual(2, system.exchange_api.get_position.call_count)
            system._resume_open_intent_position.assert_called_once()
            self.assertIsNotNone(
                system.trade_state.get_open_intent('BTCUSDT'))

    def test_absent_after_grace_rechecks_position_before_consuming_intent(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _seen_client_ids = self._system(tmp)
            system.exchange_api.get_position = Mock(side_effect=[
                None, {'side': 'long', 'contracts': 1.0}])
            system.exchange_api.find_existing_open_order = Mock(
                return_value=None)
            system._resume_open_intent_position = Mock(return_value=False)
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IABSENTRACE123',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0}, planned_position_size=1.0)

            self.assertEqual(
                {'BTCUSDT'}, system._reconcile_all_open_intents('test'))

            self.assertEqual(2, system.exchange_api.get_position.call_count)
            system._resume_open_intent_position.assert_called_once()
            self.assertIsNotNone(
                system.trade_state.get_open_intent('BTCUSDT'))

    def test_late_visible_filled_order_keeps_recent_intent_without_close_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _seen_client_ids = self._system(tmp)
            system.exchange_api.get_position = Mock(return_value=None)
            system.exchange_api._get_contract_size = lambda _symbol: 1.0
            system.exchange_api.find_existing_open_order = Mock(return_value={
                'id': 'open-late', 'status': 'closed',
                'amount': 1.0, 'filled': 1.0, 'remaining': 0.0,
                'average': 100.0,
            })
            system._recover_flat_compensation_evidence = Mock(
                return_value=None)
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'ILATEVISIBLE123',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0}, planned_position_size=1.0)

            self.assertEqual(
                {'BTCUSDT'}, system._reconcile_all_open_intents('test'))

            self.assertIsNotNone(
                system.trade_state.get_open_intent('BTCUSDT'))
            self.assertTrue(
                system.trade_state.is_position_quarantined('BTCUSDT'))
            system.exchange_api.find_existing_open_order.assert_called_once_with(
                'BTCUSDT', 'long', 1.0, 'ILATEVISIBLE123',
                wait_for_visibility=True)

    def test_disabled_symbol_still_recovers_already_existing_exchange_position(self):
        """只平不开不能吞掉真钱孤儿仓：交易所有仓时仍须补账和止损。"""
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(
                tmp, exchange_position={'side': 'long', 'contracts': 1})
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IRECOVERRETIRED',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=1.0)
            system.config['trading']['symbols'][0]['enabled'] = False

            self.assertEqual(
                {'BTCUSDT'}, system._reconcile_all_open_intents('test'))

            self.assertEqual([], seen_client_ids)
            position = system.trade_state.get_open_position('BTCUSDT')
            self.assertEqual('long', position['side'])
            self.assertTrue(position['recovered_unresolved_open'])
            self.assertIsNotNone(
                system.trade_state.get_open_intent('BTCUSDT'))

    def test_generic_executor_blocks_non_recovery_open_for_disabled_symbol(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, seen_client_ids = self._system(tmp)

            outcome = system._execute_open(
                'BTCUSDT', 'long', 100.0, 90.0,
                {'name': 'BTCUSDT', 'enabled': False,
                 'risk_per_trade': 0.01})

            self.assertEqual('retired_blocked', outcome['status'])
            self.assertEqual([], seen_client_ids)
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))

    def test_round_trip_finalizer_rejects_unproven_close_result(self):
        malformed = (
            {'fully_closed': True},
            {'confirmed': None, 'fully_closed': True},
            {'confirmed': 'true', 'fully_closed': True},
            {'confirmed': False, 'fully_closed': True},
        )
        for close_order in malformed:
            with self.subTest(close_order=close_order), \
                    tempfile.TemporaryDirectory() as tmp:
                system, _ = self._system(tmp)
                intent = system.trade_state.prepare_open_intent(
                    'BTCUSDT', 'ma_cross', 'long', 'IFINALSTRICT1',
                    {'side': 'long', 'entry_price': 100.0,
                     'stop_loss_price': 90.0},
                    planned_position_size=1.0)

                finalized = system._finalize_open_intent_rollback(
                    'BTCUSDT', intent, {
                        'status': 'rolled_back',
                        'open_order': {'id': 'open-1'},
                        'close_order': close_order,
                        'entry_price': 100.0,
                        'position_size': 1.0})

                self.assertFalse(finalized)
                self.assertIsNotNone(
                    system.trade_state.get_open_intent('BTCUSDT'))
                self.assertEqual(
                    [], system.trade_state.get_closed_trades())

    def test_round_trip_finalizer_keeps_intent_when_position_reappears(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _ = self._system(tmp)
            intent = system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IFINALLATERACE1',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0}, planned_position_size=1.0)
            system.exchange_api.get_position = Mock(return_value={
                'side': 'long', 'contracts': 1.0})

            finalized = system._finalize_open_intent_rollback(
                'BTCUSDT', intent, {
                    'status': 'rolled_back',
                    'open_order': {'id': 'open-1'},
                    'close_order': {
                        'id': 'close-1', 'confirmed': True,
                        'fully_closed': True, 'remaining_amount': 0.0,
                        'average': 99.0,
                    },
                    'entry_price': 100.0, 'position_size': 1.0,
                })

            self.assertFalse(finalized)
            self.assertIsNotNone(
                system.trade_state.get_open_intent('BTCUSDT'))
            self.assertEqual([], system.trade_state.get_closed_trades())
            self.assertTrue(
                system.trade_state.is_position_quarantined('BTCUSDT'))

    def test_round_trip_finalizer_keeps_intent_when_position_read_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _ = self._system(tmp)
            intent = system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IFINALREADFAIL1',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0}, planned_position_size=1.0)
            system.exchange_api.get_position = Mock(
                side_effect=RuntimeError('position unavailable'))

            finalized = system._finalize_open_intent_rollback(
                'BTCUSDT', intent, {
                    'status': 'rolled_back',
                    'open_order': {'id': 'open-1'},
                    'close_order': {
                        'id': 'close-1', 'confirmed': True,
                        'fully_closed': True, 'remaining_amount': 0.0,
                        'average': 99.0,
                    },
                    'entry_price': 100.0, 'position_size': 1.0,
                })

            self.assertFalse(finalized)
            self.assertIsNotNone(
                system.trade_state.get_open_intent('BTCUSDT'))
            self.assertEqual([], system.trade_state.get_closed_trades())
            self.assertTrue(
                system.trade_state.is_position_quarantined('BTCUSDT'))

    def test_generic_full_rollback_immediately_books_real_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _seen_client_ids = self._system(tmp)
            system.exchange_api.open_position = Mock(return_value={
                'id': 'open-rollback', 'average': 100.0, 'amount': 1.0,
                'confirmed': False,
                'fee': {'cost': 0.1, 'currency': 'USDT'},
                'open_execution_compensated': True,
                'compensation': {
                    'id': 'close-rollback', 'confirmed': True,
                    'fully_closed': True,
                    'average': 99.0, 'amount': 1.0,
                    'remaining_amount': 0.0,
                    'fee': {'cost': 0.2, 'currency': 'USDT'},
                },
            })

            outcome = system._execute_open(
                'BTCUSDT', 'long', 100.0, 90.0,
                {'name': 'BTCUSDT',
                 'risk_per_trade': 0.01})

            self.assertEqual('rolled_back', outcome['status'])
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            closed = system.trade_state.get_closed_trades()
            self.assertEqual(1, len(closed))
            self.assertEqual(['open-rollback'], closed[0]['entry_order_ids'])
            self.assertEqual(['close-rollback'], closed[0]['exit_order_ids'])
            self.assertEqual('actual', closed[0]['fee_source'])
            self.assertAlmostEqual(0.3, closed[0]['total_fee'])

    def test_round_trip_finalizer_rejects_quantity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _ = self._system(tmp)
            intent = system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IFINALQTYMISMATCH1',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0}, planned_position_size=1.0)
            system.exchange_api.get_position = Mock(return_value=None)

            finalized = system._finalize_open_intent_rollback(
                'BTCUSDT', intent, {
                    'status': 'rolled_back',
                    'open_order': {
                        'id': 'open-qty', 'average': 100.0, 'amount': 1.0},
                    'close_order': {
                        'id': 'close-qty', 'confirmed': True,
                        'fully_closed': True, 'remaining_amount': 0.0,
                        'average': 99.0, 'amount': 0.5},
                    'entry_price': 100.0, 'position_size': 0.5,
                })

            self.assertFalse(finalized)
            self.assertIsNotNone(
                system.trade_state.get_open_intent('BTCUSDT'))
            self.assertEqual([], system.trade_state.get_closed_trades())
            self.assertTrue(
                system.trade_state.is_position_quarantined('BTCUSDT'))

    def test_position_save_failure_books_real_compensation_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _seen_client_ids = self._system(tmp)
            system.exchange_api.close_position = Mock(return_value={
                'id': 'close-after-save-fail', 'confirmed': True,
                'fully_closed': True,
                'average': 98.0, 'amount': 1.0, 'remaining_amount': 0.0,
                'fee': {'cost': 0.2, 'currency': 'USDT'},
            })
            system.exchange_api.close_position.side_effect = lambda *args, **kwargs: (
                system._exchange_position_state.__setitem__('position', None)
                or {
                    'id': 'close-after-save-fail', 'confirmed': True,
                    'fully_closed': True, 'average': 98.0,
                    'amount': 1.0, 'remaining_amount': 0.0,
                    'fee': {'cost': 0.2, 'currency': 'USDT'},
                })
            system._cancel_stop_order_confirmed = Mock(return_value=True)
            system.trade_state.add_open_position = Mock(
                side_effect=TradeStatePersistenceError('single write failure'))

            outcome = system._execute_open(
                'BTCUSDT', 'long', 100.0, 90.0,
                {'name': 'BTCUSDT',
                 'risk_per_trade': 0.01})

            self.assertEqual('rolled_back', outcome['status'])
            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            closed = system.trade_state.get_closed_trades()
            self.assertEqual(['close-after-save-fail'], closed[0]['exit_order_ids'])
            self.assertAlmostEqual(0.2, closed[0]['exit_fee'])

    def test_recovered_intent_rollback_is_not_finalized_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            system, _seen_client_ids = self._system(
                tmp, exchange_position={'side': 'long', 'contracts': 1})
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IRECOVERROLLBACK',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0},
                planned_position_size=1.0)
            close_evidence = {
                'id': 'close-recovered', 'confirmed': True,
                'fully_closed': True, 'average': 79.0,
                'amount': 1.0, 'filled_amount': 1.0,
                'remaining_amount': 0.0,
            }
            system.exchange_api.close_position = Mock()
            system.exchange_api.find_compensation_close_progress = Mock(
                return_value=_terminal_compensation_progress(
                    'RRECOVERROLLBACK', 1.0, 1.0, 1.0,
                    order_id='close-recovered', average=79.0))
            system._recover_flat_compensation_evidence = Mock(
                return_value=close_evidence)
            system.exchange_api.get_position = Mock(side_effect=[
                {'side': 'long', 'contracts': 1.0},
                None, None, None,
            ])
            system.risk_manager.calculate_position_size = Mock(
                side_effect=AssertionError('恢复不得重算风险'))

            # 第一轮发现原单笔补偿订单已全成但入口时仓位尚可见：
            # 只保留 intent 并隔离，不重发 close。
            self.assertEqual(
                {'BTCUSDT'}, system._reconcile_all_open_intents('test'))
            self.assertIsNotNone(
                system.trade_state.get_open_intent('BTCUSDT'))
            system.exchange_api.close_position.assert_not_called()

            # 下一轮 fresh flat 时才用同一确定性补偿证据补记往返。
            self.assertEqual(set(), system._reconcile_all_open_intents('test'))

            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            self.assertEqual(1, len(system.trade_state.get_closed_trades()))
            system.exchange_api.close_position.assert_not_called()




class PositionReconciliationStateTest(unittest.TestCase):
    def _system(self, tmp):
        system = TradingSystem.__new__(TradingSystem)
        system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda symbol: symbol,
            _coin_to_contracts=lambda symbol, amount: amount * 10,
            _contracts_to_coins=lambda symbol, contracts: contracts / 10,
        )
        system.notifier = SimpleNamespace(notify_error=lambda *args, **kwargs: True)
        return system

    def test_direction_or_quantity_mismatch_is_persistently_quarantined(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            local = {
                'symbol': 'BTCUSDT', 'side': 'long', 'position_size': 1,
                'entry_price': 100, 'stop_loss_price': 90,
            }
            self.assertFalse(system._verify_existing_position_or_quarantine(
                'BTCUSDT', local, {'side': 'short', 'contracts': 10}))
            self.assertTrue(TradeState(
                os.path.join(tmp, 'trade_state.json')).is_position_quarantined('BTCUSDT'))

            self.assertTrue(system._verify_existing_position_or_quarantine(
                'BTCUSDT', local, {'side': 'long', 'contracts': 10}))
            self.assertFalse(system.trade_state.is_position_quarantined('BTCUSDT'))

    def test_quarantine_disk_failure_still_blocks_in_current_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.notifier = SimpleNamespace(notify_error=Mock())
            system.trade_state.mark_position_quarantine = Mock(
                side_effect=TradeStatePersistenceError('disk full'))

            self.assertFalse(system._quarantine_position_mismatch(
                'BTCUSDT', '方向不一致'))

            self.assertTrue(system.trade_state.is_position_quarantined('BTCUSDT'))
            system.notifier.notify_error.assert_called_once()

    def test_pending_open_intent_centrally_blocks_quarantine_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'ICLEARBLOCK123',
                {'side': 'long', 'entry_price': 100.0,
                 'stop_loss_price': 90.0}, planned_position_size=1.0)
            system.trade_state.mark_position_quarantine(
                'BTCUSDT', 'pending open remains')

            self.assertFalse(
                system._clear_position_quarantine_after_reconcile('BTCUSDT'))

            self.assertIsNotNone(
                system.trade_state.get_open_intent('BTCUSDT'))
            self.assertTrue(
                system.trade_state.is_position_quarantined('BTCUSDT'))

    def test_pending_close_intent_centrally_blocks_quarantine_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.trade_state.add_open_position(
                'BTCUSDT', 'long', 100.0, 1.0, 90.0, 'stop-1',
                strategy='ma_cross')
            system.trade_state.prepare_close_intent(
                'BTCUSDT', 'CCLEARBLOCK123', 'test close')
            system.trade_state.mark_position_quarantine(
                'BTCUSDT', 'pending close remains')

            self.assertFalse(
                system._clear_position_quarantine_after_reconcile('BTCUSDT'))

            self.assertIsNotNone(
                system.trade_state.get_close_intent('BTCUSDT'))
            self.assertTrue(
                system.trade_state.is_position_quarantined('BTCUSDT'))


class MaCatchupStateTest(unittest.TestCase):
    class _Series:
        def __init__(self, values):
            self.values = values

        def tolist(self):
            return list(self.values)

    class _ILoc:
        def __init__(self, frame):
            self.frame = frame

        def __getitem__(self, item):
            if isinstance(item, slice):
                return MaCatchupStateTest._Frame(self.frame.timestamps[item])
            raise TypeError(item)

    class _Frame:
        def __init__(self, timestamps):
            self.timestamps = list(timestamps)
            self.iloc = MaCatchupStateTest._ILoc(self)

        def __len__(self):
            return len(self.timestamps)

        def __getitem__(self, key):
            if key == 'timestamp':
                return MaCatchupStateTest._Series(self.timestamps)
            raise KeyError(key)

    class _Strategy:
        long_period = 2
        stop_loss_period = 2

        def check_current_state(self, _df):
            return {
                'action': 'long', 'ema_short': 2, 'ema_long': 1,
                'upper_stop': 12, 'lower_stop': 8, 'current_close': 11,
            }

        def check_signal(self, df):
            action = {4: 'short', 5: 'long'}.get(len(df))
            return {'action': action} if action else {'action': None}

    class _OldCrossOnlyStrategy(_Strategy):
        def check_signal(self, df):
            return {'action': 'short' if len(df) == 4 else None}

    def test_only_checks_latest_cross_instead_of_replaying_missing_bars(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.mark_candle_processed('BTCUSDT', 't3')
            system.ma_cross_strategy = self._Strategy()
            frame = self._Frame(['t1', 't2', 't3', 't4', 't5'])

            signal, candle_id = system._ma_signal_with_catchup('BTCUSDT', frame)
            self.assertEqual('long', signal['action'])
            self.assertEqual('t5', candle_id)

            system.trade_state.mark_candle_processed('BTCUSDT', 't5')
            signal, _candle_id = system._ma_signal_with_catchup('BTCUSDT', frame)
            self.assertIsNone(signal['action'])

    def test_large_history_gap_ignores_old_bars_but_keeps_latest_cross(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.mark_candle_processed('BTCUSDT', 't1')
            system.ma_cross_strategy = self._Strategy()
            frame = self._Frame(['t1', 't2', 't3', 't4', 't5'])

            signal, candle_id = system._ma_signal_with_catchup('BTCUSDT', frame)

            self.assertEqual('t5', candle_id)
            self.assertEqual('long', signal['action'])
            self.assertTrue(signal['_history_discontinuity'])
            self.assertEqual(4, signal['_history_gap_candles'])

    def test_large_history_gap_does_not_replay_an_old_cross(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.mark_candle_processed('BTCUSDT', 't1')
            system.ma_cross_strategy = self._OldCrossOnlyStrategy()
            frame = self._Frame(['t1', 't2', 't3', 't4', 't5'])

            signal, candle_id = system._ma_signal_with_catchup('BTCUSDT', frame)

            self.assertEqual('t5', candle_id)
            self.assertIsNone(signal['action'])
            self.assertTrue(signal['_history_discontinuity'])

    def test_invisible_previous_marker_still_checks_latest_cross(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.mark_candle_processed(
                'BTCUSDT', 'outside-visible-window')
            system.ma_cross_strategy = self._Strategy()
            frame = self._Frame(['t1', 't2', 't3', 't4', 't5'])

            signal, candle_id = system._ma_signal_with_catchup('BTCUSDT', frame)

            self.assertEqual('t5', candle_id)
            self.assertEqual('long', signal['action'])
            self.assertTrue(signal['_history_discontinuity'])

    def test_missing_marker_checks_latest_without_replaying_visible_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.ma_cross_strategy = self._Strategy()
            frame = self._Frame(['t1', 't2', 't3', 't4', 't5'])

            signal, candle_id = system._ma_signal_with_catchup('BTCUSDT', frame)

            self.assertEqual('t5', candle_id)
            self.assertEqual('long', signal['action'])
            self.assertTrue(signal['_history_discontinuity'])

    def test_missing_marker_requires_rebaseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            frame = self._Frame(['t1', 't2', 't3', 't4', 't5'])

            rebaseline, previous, current, gap = (
                system._history_requires_rebaseline('BTCUSDT', frame))

            self.assertTrue(rebaseline)
            self.assertIsNone(previous)
            self.assertEqual('t5', current)
            self.assertIsNone(gap)

    def test_large_gap_requires_rebaseline_but_short_gap_does_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            frame = self._Frame(['t1', 't2', 't3', 't4', 't5'])
            system.trade_state.mark_candle_processed('BTCUSDT', 't1')

            rebaseline, _, _, gap = system._history_requires_rebaseline(
                'BTCUSDT', frame)
            self.assertTrue(rebaseline)
            self.assertEqual(4, gap)

            system.trade_state.mark_candle_processed('BTCUSDT', 't2')
            rebaseline, _, _, gap = system._history_requires_rebaseline(
                'BTCUSDT', frame)
            self.assertFalse(rebaseline)
            self.assertEqual(3, gap)

    def test_sparse_rows_with_large_calendar_gap_require_rebaseline(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
            system.trade_state.mark_candle_processed(
                'BTCUSDT', '2026-05-05T00:00:00')
            frame = self._Frame([
                '2026-05-05T00:00:00',
                '2026-07-09T00:00:00',
                '2026-07-10T00:00:00',
            ])

            rebaseline, _, _, gap = system._history_requires_rebaseline(
                'BTCUSDT', frame)

            self.assertTrue(rebaseline)
            self.assertEqual(66, gap)


class DailyCandleFreshnessTest(unittest.TestCase):
    class _TimestampSeries:
        def __init__(self, values):
            self._values = values

        def tolist(self):
            return list(self._values)

    class _Frame:
        def __init__(self, values):
            self._timestamps = DailyCandleFreshnessTest._TimestampSeries(values)

        def __getitem__(self, key):
            if key == 'timestamp':
                return self._timestamps
            raise KeyError(key)

    def test_expected_previous_day_is_fresh(self):
        frame = self._Frame([datetime(2026, 7, 10)])
        fresh, latest, minimum = TradingSystem._daily_candle_is_fresh(
            frame, '2026-07-11')
        self.assertTrue(fresh)
        self.assertEqual(date(2026, 7, 10), latest)
        self.assertEqual(date(2026, 7, 10), minimum)

    def test_d_minus_two_is_rejected_for_24x7_crypto(self):
        frame = self._Frame([datetime(2026, 7, 9)])
        fresh, _, _ = TradingSystem._daily_candle_is_fresh(
            frame, '2026-07-11')
        self.assertFalse(fresh)

    def test_current_or_future_dated_candle_is_rejected(self):
        for value in (datetime(2026, 7, 11), datetime(2026, 7, 12)):
            with self.subTest(value=value):
                frame = self._Frame([value])
                fresh, _, expected = TradingSystem._daily_candle_is_fresh(
                    frame, '2026-07-11')
                self.assertFalse(fresh)
                self.assertEqual(date(2026, 7, 10), expected)

    def test_multiweek_stale_candle_is_rejected(self):
        frame = self._Frame([datetime(2026, 5, 5)])
        fresh, latest, minimum = TradingSystem._daily_candle_is_fresh(
            frame, '2026-07-11')
        self.assertFalse(fresh)
        self.assertEqual(date(2026, 5, 5), latest)
        self.assertEqual(date(2026, 7, 10), minimum)

    def test_unparseable_timestamp_fails_closed(self):
        frame = self._Frame(['not-a-timestamp'])
        fresh, latest, minimum = TradingSystem._daily_candle_is_fresh(
            frame, '2026-07-11')
        self.assertFalse(fresh)
        self.assertIsNone(latest)
        self.assertIsNone(minimum)


class IndicatorPriceFormattingTest(unittest.TestCase):
    def test_preserves_meaningful_digits_for_each_price_scale(self):
        fmt = TradingSystem._format_indicator_price
        self.assertEqual('4113.60', fmt(4113.6))
        self.assertEqual('7.9100', fmt(7.91))
        self.assertEqual('0.169064', fmt(0.169063860497983))
        self.assertEqual('0.00147774', fmt(0.00147774092545))
        self.assertEqual('0.0000051234', fmt(0.0000051234))


class MaMarkerIntegrationTest(unittest.TestCase):
    class _CloseILoc:
        def __getitem__(self, index):
            if index == -1:
                return 11.0
            raise IndexError(index)

    class _CloseSeries:
        iloc = None

        def __init__(self):
            self.iloc = MaMarkerIntegrationTest._CloseILoc()

    class _Frame:
        def __len__(self):
            return 5

        def __getitem__(self, key):
            if key == 'close':
                return MaMarkerIntegrationTest._CloseSeries()
            raise KeyError(key)

    def _system(self, tmp, *, held_side=None):
        system = TradingSystem.__new__(TradingSystem)
        # check_and_execute_trades 的真钱边界要求宿主显式绑定运行目录；
        # 绕过正式构造器的集成夹具必须复现这一约束。
        system.base_dir = tmp
        system.trade_state = TradeState(os.path.join(tmp, 'trade_state.json'))
        if held_side:
            system.trade_state.add_open_position(
                'BTCUSDT', held_side, 10.0, 1.0,
                8.0 if held_side == 'long' else 12.0,
                'stop-old', strategy='ma_cross')
        system.trade_state.mark_candle_processed('BTCUSDT', 't3')
        system.config = {
            'strategy': {
                'default_risk_per_trade': 0.01,
                'ma_short_period': 2, 'ma_long_period': 2, 'ma_stop_period': 2,
            },
            'trading': {'symbols': [{
                'name': 'BTCUSDT', 'enabled': True,
                'risk_per_trade': 0.01,
            }]},
        }
        frame = self._Frame()
        exchange_position = (
            {'side': held_side, 'contracts': 1.0} if held_side else None)
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda symbol: symbol,
            get_position=Mock(return_value=exchange_position),
            _coin_to_contracts=lambda _symbol, amount: amount,
            _contracts_to_coins=lambda _symbol, contracts: contracts,
            find_stop_order_state=lambda *args, **kwargs: 'intact',
            fetch_ohlcv=lambda *args, **kwargs: [[1]],
            ohlcv_to_dataframe=lambda _rows: frame,
            filter_closed_candles=lambda df, timeframe='1d': df,
        )
        system.ma_cross_strategy = SimpleNamespace()
        system._trade_lock = threading.Lock()
        system._last_check_date = None
        system._last_failure_notify_ts = 0
        system._pending_trade_open_notifications = []
        system._pending_trade_close_notifications = []
        system._stop_anomalies = {}
        system.stop_loss_dates = {}
        system.equity_tracker = SimpleNamespace(
            record_daily_equity_snapshot=lambda: None,
            refresh_account_stats_state=lambda: None)
        system.notifier = SimpleNamespace(
            notify_error=Mock(), notify_signal_missed=Mock(), send_message=Mock())
        system._retry_clear_stop_residues = lambda: None
        system._flush_pending_trade_notifications = lambda: None
        system.send_daily_position_summary_if_due = lambda **kwargs: True
        system._daily_candle_is_fresh = (
            lambda _df, _scheduled_date: (True, date(2026, 7, 10), date(2026, 7, 9)))
        return system

    @staticmethod
    def _signal(action='long', target='long'):
        return {
            'action': action, 'target_side': target,
            'ema_short': 2.0, 'ema_long': 1.0,
            'upper_stop': 12.0, 'lower_stop': 8.0,
            'current_close': 11.0,
        }

    def test_failed_ma_open_does_not_advance_candle_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system._ma_signal_with_catchup = (
                lambda *args, **kwargs: (self._signal(), 't5'))
            system._execute_open = Mock(return_value=None)

            system.check_and_execute_trades()

            metadata = system.trade_state.get_signal_metadata('BTCUSDT')
            self.assertEqual('t3', metadata['last_processed_candle'])
            self.assertIsNone(system._last_check_date)
            system.notifier.notify_signal_missed.assert_called_once()

    def test_same_day_stop_block_consumes_cross_without_failure_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.stop_loss_dates['BTCUSDT'] = date.today().isoformat()
            system._ma_signal_with_catchup = (
                lambda *args, **kwargs: (self._signal(), 't5'))
            system._execute_open = Mock(
                side_effect=AssertionError('T+1 当日不得再次开仓'))

            system.check_and_execute_trades()

            metadata = system.trade_state.get_signal_metadata('BTCUSDT')
            self.assertEqual('t5', metadata['last_processed_candle'])
            self.assertEqual(date.today().isoformat(), system._last_check_date)
            system._execute_open.assert_not_called()
            system.notifier.notify_signal_missed.assert_not_called()

    def test_failed_t1_reentry_keeps_candle_and_day_pending_for_intraday_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system.stop_loss_dates['BTCUSDT'] = '2000-01-01'
            system.ma_cross_strategy.check_reentry_condition = Mock(
                return_value=(True, 'long', self._signal(action=None)))
            system._ma_signal_with_catchup = (
                lambda *args, **kwargs: (self._signal(action=None), 't5'))
            system._execute_open = Mock(return_value=None)

            system.check_and_execute_trades()

            metadata = system.trade_state.get_signal_metadata('BTCUSDT')
            self.assertEqual('t3', metadata['last_processed_candle'])
            self.assertIsNone(system._last_check_date)
            self.assertEqual('2000-01-01', system.stop_loss_dates['BTCUSDT'])
            system.notifier.notify_signal_missed.assert_called_once()

    def test_invalid_ma_state_keeps_day_incomplete_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp)
            system._ma_signal_with_catchup = (
                lambda *args, **kwargs: (None, 't5'))
            system._execute_open = Mock(
                side_effect=AssertionError('无有效指标状态时不得开仓'))

            system.check_and_execute_trades()

            metadata = system.trade_state.get_signal_metadata('BTCUSDT')
            self.assertEqual('t3', metadata['last_processed_candle'])
            self.assertIsNone(system._last_check_date)
            system._execute_open.assert_not_called()
            system.notifier.send_message.assert_called_once()

    def test_opposite_held_position_does_not_flip_without_latest_cross(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = self._system(tmp, held_side='short')
            system.trade_state.mark_candle_processed('BTCUSDT', 't5')
            system._ma_signal_with_catchup = (
                lambda *args, **kwargs: (self._signal(action=None), 't5'))
            seen_actions = []

            def observe(symbol, signal, position, _config):
                seen_actions.append(signal.get('action'))

            system.handle_open_position_ma_cross = observe

            system.check_and_execute_trades()

            self.assertEqual([None], seen_actions)
            self.assertEqual('short',
                             system.trade_state.get_open_position('BTCUSDT')['side'])


class PendingOrderResolutionStrictnessTest(unittest.TestCase):
    def setUp(self):
        self.system = TradingSystem.__new__(TradingSystem)

    def test_conflicting_live_and_terminal_states_are_unresolved(self):
        terminal, _filled = self.system._pending_order_resolution({
            'status': 'closed',
            'info': {'state': 'live'},
            'amount': 1.0, 'filled': 1.0, 'remaining': 0.0,
        })
        self.assertFalse(terminal)

    def test_quantity_contradictions_are_unresolved(self):
        invalid = (
            {'status': 'closed', 'amount': 1.0,
             'filled': 2.0, 'remaining': 0.0},
            {'status': 'closed', 'amount': 1.0,
             'filled': 0.0, 'remaining': 2.0},
            {'status': 'closed', 'amount': 1.0,
             'filled': 0.8, 'remaining': 0.8},
            {'status': 'closed', 'amount': True,
             'filled': 1.0, 'remaining': 0.0},
        )
        for order in invalid:
            with self.subTest(order=order):
                self.assertEqual(
                    (False, None),
                    self.system._pending_order_resolution(order))

    def test_nonempty_consistent_terminal_state_is_accepted(self):
        self.assertEqual(
            (True, 0.4),
            self.system._pending_order_resolution({
                'status': 'canceled', 'amount': 1.0,
                'filled': 0.4, 'remaining': 0.6}))

    def test_terminal_status_never_invents_missing_filled_quantity(self):
        self.assertEqual(
            (True, None),
            self.system._pending_order_resolution({
                'status': 'closed', 'amount': 1.0,
                'info': {'state': 'filled', 'sz': '1.0'}}))

    def test_top_and_native_fill_conflict_is_unresolved(self):
        self.assertEqual(
            (False, None),
            self.system._pending_order_resolution({
                'status': 'closed', 'amount': 1.0,
                'filled': 1.0, 'remaining': 0.0,
                'info': {
                    'state': 'filled', 'sz': '1.0',
                    'accFillSz': '0.0',
                }}))


class PositionReconciliationExactQuantityTest(unittest.TestCase):
    def test_lossy_contract_rounding_cannot_hide_local_ledger_excess(self):
        system = TradingSystem.__new__(TradingSystem)
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT',
            _coin_to_contracts=lambda _symbol, amount: int(float(amount)),
            _contracts_to_coins=lambda _symbol, contracts: float(contracts),
        )

        details = system._position_reconciliation_details(
            'BTCUSDT',
            {'side': 'long', 'position_size': 10.9},
            {'side': 'long', 'contracts': 10.0},
        )

        self.assertFalse(details['quantity_match'])
        self.assertFalse(details['matched'])

    def test_large_contract_count_does_not_gain_relative_quantity_slack(self):
        system = TradingSystem.__new__(TradingSystem)
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda _symbol: 'BTC/USDT:USDT',
            _coin_to_contracts=lambda _symbol, amount: float(amount),
            _contracts_to_coins=lambda _symbol, contracts: float(contracts),
        )

        details = system._position_reconciliation_details(
            'BTCUSDT',
            {'side': 'long', 'position_size': 10_000_000_000.0},
            {'side': 'long', 'contracts': 10_000_000_000.5},
        )

        self.assertFalse(details['quantity_match'])


class FlatOpenIntentFinancialEvidenceTest(unittest.TestCase):
    def _intent(self):
        return {
            'side': 'long', 'client_order_id': 'IRECOVER123',
            'payload': {
                'side': 'long', 'entry_price': 100.0,
                'stop_loss_price': 90.0,
            },
        }

    def test_flat_without_compensation_evidence_cannot_consume_intent(self):
        system = TradingSystem.__new__(TradingSystem)
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda symbol: symbol,
            _get_contract_size=lambda _symbol: 1.0,
            get_position=Mock(return_value=None),
            get_last_price=lambda _symbol: 95.0,
        )
        system._recover_flat_compensation_evidence = Mock(return_value=None)
        system._clear_position_quarantine_after_reconcile = Mock()
        system._quarantine_position_mismatch = Mock(return_value=True)
        system.notifier = SimpleNamespace(notify_error=Mock())
        system.trade_state = SimpleNamespace(
            finalize_open_intent_round_trip=Mock())

        with self.assertRaisesRegex(RuntimeError, '缺少覆盖全部成交量'):
            system._finalize_flat_filled_open_intent(
                'BTCUSDT', self._intent(),
                {'id': 'open-proof', 'average': 100.0},
                1.0,
            )

        system.trade_state.finalize_open_intent_round_trip.assert_not_called()
        system._clear_position_quarantine_after_reconcile.assert_not_called()
        system._quarantine_position_mismatch.assert_not_called()

    def test_fresh_position_after_terminal_order_resumes_instead_of_finalizing(self):
        system = TradingSystem.__new__(TradingSystem)
        system.exchange_api = SimpleNamespace(
            to_ccxt_symbol=lambda symbol: symbol,
            _get_contract_size=lambda _symbol: 1.0,
            get_position=Mock(return_value={
                'side': 'long', 'contracts': 1.0}),
        )
        system._recover_flat_compensation_evidence = Mock()
        system._resume_open_intent_position = Mock(return_value=True)
        system._clear_position_quarantine_after_reconcile = Mock()
        system.trade_state = SimpleNamespace(
            finalize_open_intent_round_trip=Mock())

        result = system._finalize_flat_filled_open_intent(
            'BTCUSDT', self._intent(),
            {'status': 'closed', 'amount': 1.0, 'filled': 1.0},
            1.0)

        self.assertTrue(result)
        system._resume_open_intent_position.assert_called_once()
        system._recover_flat_compensation_evidence.assert_not_called()
        system.trade_state.finalize_open_intent_round_trip.assert_not_called()
        system._clear_position_quarantine_after_reconcile.assert_not_called()

    def test_strict_round_trip_persists_unknown_stop_residue_before_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = TradeState(
                os.path.join(tmp, 'trade_state.json'))
            intent = system.trade_state.prepare_open_intent(
                'BTCUSDT', 'ma_cross', 'long', 'IRECOVER123',
                self._intent()['payload'], planned_position_size=1.0)
            system.exchange_api = SimpleNamespace(
                to_ccxt_symbol=lambda symbol: symbol,
                _get_contract_size=lambda _symbol: 1.0,
                get_position=Mock(side_effect=[None, None]),
                get_last_price=lambda _symbol: 95.0,
            )
            system._recover_flat_compensation_evidence = Mock(return_value={
                'id': 'close-proof', 'confirmed': True,
                'fully_closed': True, 'remaining_amount': 0.0,
                'average': 99.0,
            })
            system.notifier = SimpleNamespace(notify_error=Mock())

            self.assertTrue(system._finalize_flat_filled_open_intent(
                'BTCUSDT', intent,
                {'id': 'open-proof', 'average': 100.0},
                1.0))

            self.assertIsNone(system.trade_state.get_open_intent('BTCUSDT'))
            closed = system.trade_state.get_closed_trades()
            self.assertEqual(1, len(closed))
            # 未经适配层财务净化的 raw price 不得穿透成入场价。
            self.assertEqual(100.0, closed[0]['entry_price'])
            self.assertTrue(system.trade_state.has_stop_residue('BTCUSDT'))
            self.assertTrue(
                system.trade_state.is_position_quarantined('BTCUSDT'))


class CrossMidnightScheduleTest(unittest.TestCase):
    def _system(self, check_hour=23, check_minute=59):
        system = TradingSystem.__new__(TradingSystem)
        system.config = {'scheduler': {
            'check_hour': check_hour, 'check_minute': check_minute}}
        system.label = '欧易'
        system._last_check_date = None
        calls = []
        system.check_and_execute_trades = (
            lambda **kwargs: calls.append(kwargs.get('scheduled_date')))
        return system, calls

    def test_2359_buffer_and_catchup_keep_previous_schedule_date(self):
        system, calls = self._system()
        system._run_startup_catchup_check(now=datetime(2026, 7, 11, 0, 0))
        self.assertEqual([], calls)
        system._run_startup_catchup_check(now=datetime(2026, 7, 11, 0, 2))
        self.assertEqual(['2026-07-10'], calls)

    def test_daily_health_readiness_observes_buffer_and_completion(self):
        system, _calls = self._system(check_hour=8, check_minute=0)
        system._last_check_date = '2026-07-10'
        self.assertEqual(
            (False, '2026-07-10'),
            system._daily_check_readiness(datetime(2026, 7, 11, 8, 1, 59)))

        self.assertEqual(
            (True, '2026-07-11'),
            system._daily_check_readiness(datetime(2026, 7, 11, 8, 2)))

        system._last_check_date = '2026-07-11'
        self.assertEqual(
            (False, '2026-07-11'),
            system._daily_check_readiness(datetime(2026, 7, 11, 8, 2)))

    def test_failed_daily_check_stays_overdue_until_next_morning(self):
        system, _calls = self._system(check_hour=8, check_minute=0)
        system._last_check_date = '2026-07-10'
        self.assertEqual(
            (True, '2026-07-11'),
            system._daily_check_readiness(datetime(2026, 7, 12, 7, 59)))

    def test_daily_health_readiness_binds_cross_midnight_slot(self):
        system, _calls = self._system(check_hour=23, check_minute=59)
        self.assertEqual(
            (True, '2026-07-10'),
            system._daily_check_readiness(datetime(2026, 7, 11, 0, 1)))
        system._last_check_date = '2026-07-10'
        self.assertEqual(
            (False, '2026-07-10'),
            system._daily_check_readiness(datetime(2026, 7, 11, 0, 1)))

    def test_cross_midnight_retry_uses_previous_schedule_date(self):
        system, calls = self._system()
        system._run_daily_check_retry(now=datetime(2026, 7, 11, 0, 0))
        self.assertEqual(['2026-07-10'], calls)

    def test_cross_midnight_summary_retry_uses_previous_schedule_date(self):
        system, _calls = self._system()
        system.config['scheduler'].update({'summary_hour': 23, 'summary_minute': 59})
        summary_dates = []
        system.send_daily_position_summary_if_due = (
            lambda **kwargs: summary_dates.append(kwargs.get('summary_date')))
        system._run_daily_summary_retry(now=datetime(2026, 7, 11, 0, 0))
        self.assertEqual(['2026-07-10'], summary_dates)

    def test_registers_2359_retries_at_next_day_midnight(self):
        system, _calls = self._system()
        system.exchange_id = 'okx'
        jobs = {}
        system.scheduler = SimpleNamespace(
            add_job=lambda func, trigger, **kwargs: jobs.__setitem__(kwargs['id'], kwargs))
        system._record_equity_tick_with_alert = lambda: None
        system.config['scheduler'].update({'summary_hour': 23, 'summary_minute': 59})
        system.register_jobs(system.config['scheduler'])
        self.assertEqual(0, jobs['okx_daily_check_retry']['hour'])
        self.assertEqual(0, jobs['okx_daily_check_retry']['minute'])
        self.assertEqual(0, jobs['okx_daily_summary_retry']['hour'])
        self.assertEqual(0, jobs['okx_daily_summary_retry']['minute'])

    def test_registers_0859_retry_at_0900(self):
        system, _calls = self._system(check_hour=8, check_minute=59)
        system.exchange_id = 'okx'
        jobs = {}
        system.scheduler = SimpleNamespace(
            add_job=lambda func, trigger, **kwargs: jobs.__setitem__(kwargs['id'], kwargs))
        system._record_equity_tick_with_alert = lambda: None
        system.register_jobs(system.config['scheduler'])
        self.assertEqual(9, jobs['okx_daily_check_retry']['hour'])
        self.assertEqual(0, jobs['okx_daily_check_retry']['minute'])


class MigrationAndT1FailClosedTest(unittest.TestCase):
    @staticmethod
    def _write_state(path, positions):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump({'open_positions': positions, 'closed_trades': []}, handle)

    def test_unmarked_legacy_state_keeps_original_root_and_refuses_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, 'trade_state.json')
            legacy = os.path.join(tmp, 'data', 'okx', 'trade_state.json')
            self._write_state(root, {})
            position = {
                'BTCUSDT': {
                    'symbol': 'BTCUSDT', 'side': 'long', 'entry_price': 100,
                    'position_size': 1, 'stop_loss_price': 90,
                }}
            self._write_state(legacy, position)
            system = TradingSystem.__new__(TradingSystem)
            system.base_dir = tmp
            with self.assertRaises(RuntimeError):
                system._migrate_okx_legacy_state()
            with open(root, encoding='utf-8') as handle:
                self.assertEqual({}, json.load(handle)['open_positions'])

    def test_corrupt_legacy_t1_refuses_empty_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            state.claim_owner_exchange('okx')
            t1_path = os.path.join(tmp, 'stop_loss_dates.json')
            with open(t1_path, 'w', encoding='utf-8') as handle:
                handle.write('[]')
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = state
            system.stop_loss_file = t1_path
            with self.assertRaises(TradeStatePersistenceError):
                system._load_stop_loss_dates()

    def test_broken_symlink_legacy_t1_refuses_empty_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = TradeState(os.path.join(tmp, 'trade_state.json'))
            state.claim_owner_exchange('okx')
            t1_path = os.path.join(tmp, 'stop_loss_dates.json')
            os.symlink(os.path.join(tmp, 'missing-target.json'), t1_path)
            system = TradingSystem.__new__(TradingSystem)
            system.trade_state = state
            system.stop_loss_file = t1_path

            with self.assertRaises(TradeStatePersistenceError):
                system._load_stop_loss_dates()
            self.assertFalse(state.stop_loss_dates_migrated())


class ConfigSecretPersistenceTest(unittest.TestCase):
    def test_environment_okx_credentials_never_spill_to_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'config.json')
            with open(path, 'w', encoding='utf-8') as handle:
                json.dump({
                    'okx': {'sandbox': True},
                    'strategy': {'default_risk_per_trade': 0.01},
                    'trading': {'symbols': []},
                }, handle)
            system = TradingSystem.__new__(TradingSystem)
            with patch.dict(os.environ, {
                    'OKX_API_KEY': 'env-key',
                    'OKX_API_SECRET': 'env-secret',
                    'OKX_API_PASSPHRASE': 'env-pass',
            }, clear=False):
                system.config = system.load_config(path)
            self.assertEqual('env-key', system.config['okx']['apiKey'])
            system.config_file = path
            system._config_lock = threading.RLock()
            self.assertTrue(system.persist_config())
            with open(path, encoding='utf-8') as handle:
                persisted = json.load(handle)
            self.assertNotIn('apiKey', persisted['okx'])
            self.assertNotIn('secret', persisted['okx'])
            self.assertNotIn('password', persisted['okx'])
            self.assertEqual(0o600, os.stat(path).st_mode & 0o777)

    def test_config_symlink_is_rejected_before_credentials_are_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, 'real-config.json')
            with open(target, 'w', encoding='utf-8') as handle:
                json.dump({
                    'okx': {
                        'apiKey': 'disk-key', 'secret': 'disk-secret',
                        'password': 'disk-pass',
                    },
                    'strategy': {
                        'default_risk_per_trade': 0.01,
                    },
                    'trading': {'symbols': []},
                }, handle)
            link = os.path.join(tmp, 'config.json')
            os.symlink(target, link)
            system = TradingSystem.__new__(TradingSystem)

            with self.assertRaises(ValueError):
                system.load_config(link)


if __name__ == '__main__':
    unittest.main()
