const test = require('node:test');
const assert = require('node:assert/strict');
const { placeTimeLabels } = require('../../src/n6705c_seq/web/layout.js');

function check(spans, left, width) {
  const labels = placeTimeLabels(spans, left, width);
  assert.equal(labels.length, spans.length);
  labels.forEach((label, i) => {
    assert.ok(Number.isFinite(label.x));
    assert.ok(Number.isInteger(label.row) && label.row >= 0 && label.row <= spans.length);
    const start = label.x - spans[i].width / 2;
    assert.ok(start >= left + 4);
    if (spans[i].width <= width - 8) {
      assert.ok(label.x + spans[i].width / 2 <= left + width - 4);
    } else {
      assert.equal(label.row, spans.length);
    }
    labels.slice(0, i).forEach((other, j) => {
      if (other.row === label.row) {
        assert.ok(other.x + spans[j].width / 2 + 4 <= start);
      }
    });
  });
  return labels;
}

test('empty and single labels', () => {
  assert.deepEqual(placeTimeLabels([], 10, 100), []);
  assert.deepEqual(check([{ start: 10, end: 110, width: 20 }], 10, 100), [{ x: 60, row: 0 }]);
  assert.deepEqual(check([{ start: 10, end: 11, width: 20 }], 10, 100), [{ x: 24, row: 1 }]);
});

test('tiny spans move below labels fitting their spans', () => {
  const labels = check([
    { start: 100, end: 101, width: 65 },
    { start: 101, end: 220, width: 100 },
    { start: 220, end: 330, width: 80 },
  ], 0, 330);
  assert.equal(labels[0].row, 1);
  assert.equal(labels[1].row, 0);
});

test('crowded and outlying labels stay in bounds without overlap', () => {
  check(Array.from({ length: 30 }, (_, i) => ({ start: i - 500, end: i - 499, width: 80 })), 10, 100);
  check(Array.from({ length: 30 }, (_, i) => ({ start: i + 500, end: i + 501, width: 80 })), 10, 100);
});

test('oversized labels terminate at the row cap and allow overflow', { timeout: 1000 }, () => {
  check([{ start: 0, end: 1, width: 300 }], 20, 100);
  check(Array.from({ length: 50 }, () => ({ start: 30, end: 31, width: 300 })), 20, 100);
  check([{ start: 0, end: 1, width: 92 }, { start: 0, end: 1, width: 93 }], 20, 100);
  assert.deepEqual(placeTimeLabels([
    { start: 0, end: 1, width: 300 },
    { start: 0, end: 1, width: 300 },
  ], 20, 100), [{ x: 174, row: 2 }, { x: 478, row: 2 }]);
});

// Straight port of the terminating tk loop; fixtures must fit the chart width.
function tkPlaceTimeLabels(spans, left, width) {
  const rowEnds = new Map();
  return spans.map(({ start, end, width: labelWidth }) => {
    let row = labelWidth + 4 <= end - start ? 0 : 1;
    let center = (start + end) / 2;
    let labelLeft;
    while (true) {
      labelLeft = Math.max(left + 4, Math.min(center - labelWidth / 2,
                                            left + width - 4 - labelWidth));
      labelLeft = Math.max(labelLeft, (rowEnds.get(row) ?? left) + 4);
      if (labelLeft + labelWidth <= left + width - 4) break;
      row += 1;
    }
    center = labelLeft + labelWidth / 2;
    rowEnds.set(row, labelLeft + labelWidth);
    return { x: center, row };
  });
}

test('placement matches the tk loop for terminating inputs', () => {
  const cases = [
    [{ start: 10, end: 11, width: 20 }],
    [{ start: 10, end: 110, width: 20 }],
    [{ start: 10, end: 30, width: 12 }, { start: 30, end: 31, width: 60 },
     { start: 31, end: 80, width: 40 }, { start: 80, end: 110, width: 20 }],
    Array.from({ length: 8 }, (_, i) => ({ start: i, end: i + 1, width: 80 })),
    Array.from({ length: 8 }, (_, i) => ({ start: i + 500, end: i + 501, width: 80 })),
  ];
  for (const spans of cases) {
    assert.deepEqual(placeTimeLabels(spans, 10, 100), tkPlaceTimeLabels(spans, 10, 100));
  }
});
