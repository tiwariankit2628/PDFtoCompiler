/* Progressive cell editor. The submitted representation remains plain JSON. */
(function () {
  function init(textarea) {
    const form = textarea.form, container = document.createElement('div');
    const grid = document.createElement('div'), toolbar = document.createElement('div');
    const advanced = document.createElement('details'), summary = document.createElement('summary');
    const question = form.querySelector('[name="question_text"]');
    container.className = 'structured-table-editor'; grid.className = 'table-editor-grid'; toolbar.className = 'table-editor-toolbar';
    summary.textContent = 'Advanced table JSON'; advanced.append(summary);
    textarea.parentNode.insertBefore(container, textarea); container.append(toolbar, grid, advanced); advanced.append(textarea);
    const add = document.createElement('button'); add.type = 'button'; add.className = 'btn btn-sm btn-outline-primary'; add.textContent = 'Add table';
    toolbar.append(add);
    let tables = [];
    function read() { try { const data = JSON.parse(textarea.value || '[]'); return Array.isArray(data) ? data : []; } catch (_) { return null; } }
    function sync() { textarea.value = JSON.stringify(tables, null, 2); }
    function render() {
      const data = read(); grid.replaceChildren();
      if (!data) { grid.textContent = 'Invalid table JSON. Correct it in the advanced editor.'; return; }
      tables = data;
      tables.forEach((table, ti) => {
        if (!table || !Array.isArray(table.rows)) return;
        const caption = document.createElement('p'); caption.className = 'fw-semibold mb-2'; caption.textContent = 'Table ' + (ti + 1) + ' · edit each cell'; grid.append(caption);
        const node = document.createElement('table'), body = document.createElement('tbody');
        table.rows.forEach((row, ri) => {
          if (!Array.isArray(row)) return;
          const tr = document.createElement('tr');
          row.forEach((cell, ci) => {
            if (!cell || typeof cell.text !== 'string') return;
            const td = document.createElement(cell.header ? 'th' : 'td');
            td.colSpan = Math.max(1, Math.min(20, Number(cell.colspan) || 1));
            td.rowSpan = Math.max(1, Math.min(100, Number(cell.rowspan) || 1));
            const field = document.createElement('textarea'); field.className = 'form-control form-control-sm';
            field.value = cell.text; field.setAttribute('aria-label', `Table ${ti + 1}, row ${ri + 1}, cell ${ci + 1}`);
            field.addEventListener('input', () => { tables[ti].rows[ri][ci].text = field.value; sync(); });
            td.append(field); tr.append(td);
          }); body.append(tr);
        }); node.append(body); grid.append(node);
        const rowControls = document.createElement('div'); rowControls.className = 'd-flex flex-wrap gap-2 my-2';
        const addRow = document.createElement('button'); addRow.type = 'button'; addRow.className = 'btn btn-sm btn-outline-secondary'; addRow.textContent = 'Add row';
        addRow.addEventListener('click', () => {
          if (!Number.isInteger(table.columns) || table.columns < 1 || table.columns > 20 || table.rows.length >= 100) return;
          table.rows.push(Array.from({length: table.columns}, () => ({text: '', colspan: 1, rowspan: 1, header: false})));
          sync(); render();
        });
        const removeRow = document.createElement('button'); removeRow.type = 'button'; removeRow.className = 'btn btn-sm btn-outline-secondary'; removeRow.textContent = 'Remove last row';
        removeRow.addEventListener('click', () => {
          if (table.rows.length <= 1) return;
          if (table.rows.some(row => row.some(cell => Number(cell.rowspan) > 1))) { window.alert('Use Advanced table JSON to edit rows with merged vertical cells.'); return; }
          table.rows.pop(); sync(); render();
        });
        rowControls.append(addRow, removeRow); grid.append(rowControls);
        const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'btn btn-sm btn-outline-danger my-2';
        remove.textContent = 'Remove this table';
        remove.addEventListener('click', () => {
          if (!window.confirm('Remove this table and its marker from the question?')) return;
          tables.splice(ti, 1);
          if (question) question.value = question.value.replace(/\[\[table:(\d+)\]\]/gi, (m, n) => Number(n) === ti + 1 ? '' : Number(n) > ti + 1 ? '[[table:' + (Number(n) - 1) + ']]' : m);
          sync(); render();
        }); grid.append(remove);
      });
    }
    add.addEventListener('click', () => {
      if (tables.length >= 20) return;
      tables.push({columns: 2, rows: [[{text: '', colspan: 1, rowspan: 1, header: true}, {text: '', colspan: 1, rowspan: 1, header: true}], [{text: '', colspan: 1, rowspan: 1, header: false}, {text: '', colspan: 1, rowspan: 1, header: false}]]});
      if (question) question.value += '\n[[table:' + tables.length + ']]\n'; sync(); render();
    });
    textarea.addEventListener('input', render); render();
  }
  document.addEventListener('DOMContentLoaded', () => document.querySelectorAll('[data-table-editor]').forEach(init));
})();
