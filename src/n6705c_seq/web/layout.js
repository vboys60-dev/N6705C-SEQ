function placeTimeLabels(spans, left, width) {
  const rowEnds = new Map();
  return spans.map(span => {
    const labelWidth = span.width;
    let row = labelWidth + 4 <= span.end - span.start ? 0 : 1;
    const center = (span.start + span.end) / 2;
    let labelLeft;
    while (true) {
      labelLeft = Math.max(left + 4, Math.min(center - labelWidth / 2,
                                            left + width - 4 - labelWidth));
      labelLeft = Math.max(labelLeft, (rowEnds.get(row) ?? left) + 4);
      if (labelLeft + labelWidth <= left + width - 4) break;
      if (row === spans.length) {
        labelLeft = Math.max(left + 4, (rowEnds.get(row) ?? left) + 4);
        break;
      }
      row += 1;
    }
    rowEnds.set(row, labelLeft + labelWidth);
    return { x: labelLeft + labelWidth / 2, row };
  });
}

if (typeof module !== "undefined") module.exports = { placeTimeLabels };
