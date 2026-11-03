/* Render untrusted question content with DOM APIs; never interpret PDF text as HTML. */
(function () {
  const label = /\b(?:Assertion\s*(?:[A-Z]|\([A-Z]\))?|Reason\s*(?:[A-Z]|\([A-Z]\))?|Statement\s*(?:I{1,3}|[1-3]))\s*:/gi;
  function appendText(parent, text) {
    let last = 0, match;
    label.lastIndex = 0;
    while ((match = label.exec(text))) {
      if (/(?:labelled|labeled)\s+as\s+$/i.test(text.slice(Math.max(0, match.index - 25), match.index))) continue;
      parent.append(document.createTextNode(text.slice(last, match.index)));
      const strong = document.createElement('strong');
      strong.className = 'statement-label'; strong.textContent = match[0]; parent.append(strong);
      last = label.lastIndex;
    }
    parent.append(document.createTextNode(text.slice(last)));
  }
  function makeTable(data) {
    if (!data || !Array.isArray(data.rows) || data.rows.length > 100 || !Number.isInteger(data.columns) || data.columns < 1 || data.columns > 20) return null;
    const wrapper = document.createElement('div'), table = document.createElement('table'), body = document.createElement('tbody');
    wrapper.className = 'question-table-wrap'; table.className = 'question-table';
    for (const row of data.rows) {
      if (!Array.isArray(row) || row.length > 20) return null;
      const tr = document.createElement('tr');
      for (const cell of row) {
        if (!cell || typeof cell.text !== 'string' || cell.text.length > 4000) return null;
        const node = document.createElement(cell.header === true ? 'th' : 'td');
        node.colSpan = Number.isInteger(cell.colspan) && cell.colspan > 0 && cell.colspan <= 20 ? cell.colspan : 1;
        node.rowSpan = Number.isInteger(cell.rowspan) && cell.rowspan > 0 && cell.rowspan <= 100 ? cell.rowspan : 1;
        appendText(node, cell.text); tr.append(node);
      }
      body.append(tr);
    }
    table.append(body); wrapper.append(table); return wrapper;
  }
  window.renderQuestionContent = function (target, text, tables) {
    target.replaceChildren(); text = typeof text === 'string' ? text : '';
    const pattern = /\[\[table:(\d+)\]\]/gi;
    let start = 0, match;
    while ((match = pattern.exec(text))) {
      appendText(target, text.slice(start, match.index));
      const table = makeTable(Array.isArray(tables) ? tables[Number(match[1]) - 1] : null);
      if (table) target.append(table); else target.append(document.createTextNode(match[0]));
      start = pattern.lastIndex;
    }
    appendText(target, text.slice(start));
  };
})();
