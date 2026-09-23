'use strict';
// Shared bulk controls for the cloud and local integration dialogs.
window.modelSelectionControls = function (list) {
  const bar = document.createElement('div');
  bar.className = 'model-selection-actions';
  const count = document.createElement('span');
  count.setAttribute('aria-live', 'polite');
  const all = document.createElement('button');
  const none = document.createElement('button');
  all.type = none.type = 'button';
  all.className = none.className = 'secondary mini';
  all.textContent = '全选';
  none.textContent = '取消全选';
  const inputs = () => [...list.querySelectorAll('input[type="checkbox"]:not(:disabled)')];
  const update = () => {
    const items = inputs();
    const selected = items.filter(input => input.checked).length;
    count.textContent = `已选 ${selected} / ${items.length}`;
    all.disabled = selected === items.length;
    none.disabled = selected === 0;
  };
  const select = checked => {
    inputs().forEach(input => { input.checked = checked; });
    list.dispatchEvent(new Event('change', { bubbles: true }));
  };
  all.addEventListener('click', () => select(true));
  none.addEventListener('click', () => select(false));
  list.onchange = update;
  bar.append(all, none, count);
  update();
  return bar;
};
