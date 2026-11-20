/**
 * Google Translate `tk` 令牌 —— 官方前端 JS 算法的参考实现。
 *
 * 用途：作为 Python 移植版 `imgtrans/translator/google_tk.py` 的交叉验证基准。
 * 本文件刻意保持与 Google 前端 `TL()` / `RL()` 原始代码逐行同构（含变量名风格），
 * 不做任何"优化"，以便出差异时能直接对位排查。
 *
 * 用法：
 *   echo '["hello","你好"]' | node tools/tk_reference.js
 *   node tools/tk_reference.js '["hello"]'
 *
 * 输出（UTF-8 JSON）：{"hello": "tk...", "你好": "tk..."}
 *
 * TKK 默认与 PHP 版 `TKK()` 一致：406398 + "." + (561666268 + 1526272306)，
 * 可用环境变量 TKK 覆盖（如 "406398.2087938574"）。
 */

'use strict';

const fs = require('fs');

const TKK = process.env.TKK || '406398.2087938574';

/**
 * 无符号 32 位右移，等价于 PHP 的 shr32()。
 * @param {number} a 被移位数
 * @param {number} b 位移量
 * @returns {number} 无符号右移结果（0 .. 2^32-1）
 */
function shr32(a, b) {
  if (b <= 0) return a >>> 0;
  if (b >= 32) return 0;
  return (a >>> b) >>> 0;
}

/**
 * 指令串变换，等价于 Google 官方 RL()。
 * @param {number} a 当前值
 * @param {string} b 指令串，如 "+-a^+6"
 * @returns {number} 变换结果（按 JS int32 表示）
 */
function RL(a, b) {
  const t = 'a';
  const Yb = '+';
  for (let c = 0; c < b.length - 2; c += 3) {
    let d = b.charAt(c + 2);
    d = d >= t ? d.charCodeAt(0) - 87 : Number(d);
    d = b.charAt(c + 1) === Yb ? shr32(a, d) : a << d;
    a = b.charAt(c) === Yb ? (a + d) & 4294967295 : a ^ d;
  }
  return a;
}

/**
 * 计算 tk 令牌，等价于 Google 官方 TL()（PHP 版 Google::TL() 同一算法）。
 * @param {string} text 原文；批量时是所有文本的拼接串
 * @returns {string} 形如 "123456.789012"
 */
function TL(text) {
  const parts = TKK.split('.');
  const b = Number(parts[0]);
  const d = [];
  let e = 0;

  for (let f = 0; f < text.length; f++) {
    const g = text.charCodeAt(f);
    if (g < 128) {
      d[e++] = g;
    } else if (g < 2048) {
      d[e++] = (g >> 6) | 192;
      d[e++] = (g & 63) | 128;
    } else if (55296 == (g & 64512) && f + 1 < text.length && 56320 == (text.charCodeAt(f + 1) & 64512)) {
      // 代理对 → 合成为 astral 码点 → 四字节 UTF-8
      const cp = 65536 + ((g & 1023) << 10) + (text.charCodeAt(++f) & 1023);
      d[e++] = (cp >> 18) | 240;
      d[e++] = ((cp >> 12) & 63) | 128;
      d[e++] = ((cp >> 6) & 63) | 128;
      d[e++] = (cp & 63) | 128;
    } else {
      d[e++] = (g >> 12) | 224;
      d[e++] = ((g >> 6) & 63) | 128;
      d[e++] = (g & 63) | 128;
    }
  }

  let a = b;
  for (e = 0; e < d.length; e++) {
    a += d[e];
    a = RL(a, '+-a^+6');
  }
  a = RL(a, '+-3^+b+-f');
  a ^= Number(parts[1]);
  if (a < 0) a = (a & 2147483647) + 2147483648;
  a = a % 1000000;
  return a + '.' + (a ^ b);
}

/**
 * 读取输入：优先 argv[2]，否则读 stdin。
 * @returns {string[]}
 */
function readInputs() {
  let raw = '';
  if (process.argv.length > 2) {
    raw = process.argv[2];
  } else {
    raw = fs.readFileSync(0, 'utf8');
  }
  raw = raw.trim();
  if (!raw) return [];
  const parsed = JSON.parse(raw);
  if (!Array.isArray(parsed)) {
    throw new Error('input must be a JSON array of strings');
  }
  return parsed.map((item) => String(item));
}

function main() {
  const inputs = readInputs();
  const out = {};
  for (const text of inputs) {
    out[text] = TL(text);
  }
  process.stdout.write(JSON.stringify(out));
}

if (require.main === module) {
  main();
}

module.exports = { TL, RL, shr32, TKK };
