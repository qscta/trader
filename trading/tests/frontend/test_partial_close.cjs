// 离线前端单测：执行真实 app.js；DOM、输入框和 HTTP 均为内存替身。
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../../static/app.js'), 'utf8');

function setup(inputs) {
    const calls = [], alerts = [], prompts = [];
    const ctx = vm.createContext({window: {}, document: {addEventListener() {}},
        prompt: (message) => { prompts.push(message); return inputs.shift(); },
        console, setTimeout, clearTimeout});
    vm.runInContext(source, ctx);
    ctx.showAlert = (message, type) => alerts.push({message, type});
    for (const name of ['loadPositions', 'refreshStatus', 'loadAccountStats', 'loadTrades']) ctx[name] = () => {};
    ctx.postJSON = async (url, body) => {
        calls.push({url, body});
        return {ok: true, json: async () => url.endsWith('/preview') ? {
            quote: {side: 'long', original_size: 100, reduce_size: 30, remaining_size: 70, stop_price: 80},
            token: 'signed-offline-token'
        } : {filled_size: 30, remaining_size: 70}};
    };
    return {ctx, calls, alerts, prompts};
}

test('明确确认后只发送签名 token，不让前端自行发送数量', async () => {
    const t = setup(['30', 'BTCUSDT']);
    await t.ctx.reducePosition('BTCUSDT');
    assert.equal(t.calls.length, 2);
    assert.equal(t.calls[0].body.percent, 30);
    assert.deepEqual(Object.keys(t.calls[1].body), ['token']);
    assert.equal(t.calls[1].body.token, 'signed-offline-token');
    assert.match(t.prompts[1], /剩余：70 币/);
    assert.match(t.prompts[1], /不修改下次开仓风险度/);
    assert.equal(t.alerts[0].type, 'success');
});

test('取消、非法比例、错误品种确认都不发送执行请求', async () => {
    for (const inputs of [[null], ['100'], ['0'], ['NaN'], [''], ['30', null], ['30', 'ETHUSDT']]) {
        const t = setup(inputs);
        await t.ctx.reducePosition('BTCUSDT');
        assert.equal(t.calls.filter(c => c.url === '/api/reduce_position').length, 0);
    }
});

test('快速双击只有一次预览和一次执行', async () => {
    const t = setup(['30', 'BTCUSDT']);
    await Promise.all([t.ctx.reducePosition('BTCUSDT'), t.ctx.reducePosition('BTCUSDT')]);
    assert.equal(t.calls.length, 2);
});

test('执行响应丢失不会自动重发，提示人工核对', async () => {
    const t = setup(['30', 'BTCUSDT']);
    const post = t.ctx.postJSON;
    t.ctx.postJSON = async (url, body) => {
        const response = await post(url, body);
        if (!url.endsWith('/preview')) throw new Error('offline response lost');
        return response;
    };
    await t.ctx.reducePosition('BTCUSDT');
    assert.equal(t.calls.length, 2);
    assert.match(t.alerts[0].message, /勿重复提交/);
});
