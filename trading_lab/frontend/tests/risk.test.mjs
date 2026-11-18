import test from 'node:test';
import assert from 'node:assert/strict';
import { estimateMaxRiskQuantity } from '../src/utils.ts';

const defaultFees = {
  commissionRate: 0.00025,
  minimumCommission: 5,
  stampDutyRate: 0.0005,
  transferFeeRate: 0.00001,
};

test('ETF risk uses minimum commission once per side and excludes stock-only fees', () => {
  assert.equal(estimateMaxRiskQuantity(0.65, 0.64, 200, 20_000, defaultFees, false), 19_000);
});

test('stock risk applies minimum commission, stamp duty, and transfer fees without double counting', () => {
  assert.equal(estimateMaxRiskQuantity(10, 9.8, 200, 20_000, defaultFees, true), 900);
});

test('available cash caps otherwise affordable risk lots', () => {
  assert.equal(estimateMaxRiskQuantity(0.65, 0.64, 10_000, 650, defaultFees, false), 900);
});
