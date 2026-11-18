/* Only guide entry: server validation and explicit confirmation stay authoritative. */
(() => {
  const type = document.querySelector('[name="detected_type"], [name="question_type"]');
  if (!type) return;
  const fields = {
    correct_option: kind => kind === 'mcq_single',
    typed_key: kind => kind === 'mcq_multiple' || kind === 'numerical',
    multi_scoring_mode: kind => kind === 'mcq_multiple',
    partial_credit_per_choice: kind => kind === 'mcq_multiple',
    numeric_tolerance: kind => kind === 'numerical',
  };
  function update() {
    Object.entries(fields).forEach(([name, show]) => {
      const wrapper = document.querySelector(`[data-question-field="${name}"]`);
      if (!wrapper) return;
      const visible = show(type.value);
      wrapper.hidden = !visible;
      wrapper.querySelectorAll('input, select, textarea').forEach(input => { input.disabled = !visible; });
    });
  }
  type.addEventListener('change', update);
  update();
})();
