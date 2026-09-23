'use strict';
// One keyboard-accessible picker for both workspaces; manual model IDs remain editable.
for (const id of ['test-model','default-model']) {
  const input=document.getElementById(id);
  input.removeAttribute('list');input.autocomplete='off';
  const wrap=document.createElement('div');wrap.className='model-picker';
  input.before(wrap);wrap.append(input);
  const toggle=document.createElement('button');toggle.type='button';toggle.className='picker-toggle';
  toggle.setAttribute('aria-label','展开模型列表');toggle.innerHTML='<span aria-hidden="true">⌄</span>';wrap.append(toggle);
  const list=document.createElement('div');list.id=id+'-choices';list.className='model-choices';list.hidden=true;
  list.setAttribute('role','listbox');list.setAttribute('aria-label','可用模型');document.body.append(list);
  input.setAttribute('role','combobox');input.setAttribute('aria-autocomplete','list');input.setAttribute('aria-expanded','false');input.setAttribute('aria-controls',list.id);
  let choices=[],active=-1,search='';
  const close=()=>{list.hidden=true;input.setAttribute('aria-expanded','false');input.removeAttribute('aria-activedescendant');};
  const position=()=>{const rect=wrap.getBoundingClientRect();const below=innerHeight-rect.bottom-12,above=rect.top-12;const up=below<180&&above>below;list.style.left=rect.left+'px';list.style.width=rect.width+'px';list.style.maxHeight=Math.min(280,Math.max(100,up?above:below))+'px';list.style.top=up?'auto':rect.bottom+6+'px';list.style.bottom=up?innerHeight-rect.top+6+'px':'auto';};
  const highlight=()=>{[...list.querySelectorAll('[role=option]')].forEach((option,i)=>{option.classList.toggle('active',i===active);option.setAttribute('aria-selected',String(i===active));});const option=list.children[active];if(active>=0&&option){input.setAttribute('aria-activedescendant',option.id);option.scrollIntoView({block:'nearest'});}else input.removeAttribute('aria-activedescendant');};
  const choose=index=>{if(!choices[index])return;input.value=choices[index].id;close();input.dispatchEvent(new Event('input',{bubbles:true}));close();input.focus();};
  const draw=()=>{
    choices=state.accounts.flatMap(a=>a.models).filter(m=>(id!=='test-model'||modelKind(m)===callMode)&&m.id.toLowerCase().includes(search));
    list.replaceChildren();active=-1;
    choices.forEach((model,index)=>{const option=document.createElement('div');option.id=list.id+'-'+index;option.setAttribute('role','option');option.className='model-choice';
      const name=document.createElement('span');name.textContent=model.id;const kind=document.createElement('small');kind.textContent={text:'文本',image:'图片',video:'视频'}[modelKind(model)];option.append(name,kind);
      option.addEventListener('pointerdown',e=>e.preventDefault());option.addEventListener('click',()=>choose(index));list.append(option);
    });
    if(!choices.length){const empty=document.createElement('p');empty.className='picker-empty';empty.textContent=search?'没有匹配的模型，可直接输入模型 ID':'此类型暂无可用模型';list.append(empty);}
    position();
  };
  const open=()=>{search='';list.hidden=false;input.setAttribute('aria-expanded','true');draw();};
  toggle.addEventListener('click',()=>{const wasOpen=!list.hidden;input.focus();if(wasOpen)close();else open();});
  input.addEventListener('click',open);
  input.addEventListener('input',()=>{search=input.value.toLowerCase();list.hidden=false;input.setAttribute('aria-expanded','true');draw();});
  input.addEventListener('keydown',e=>{
    if(e.key==='Escape'){close();return;}if(e.key==='Tab'){close();return;}
    if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();if(list.hidden)open();active=choices.length?(active+(e.key==='ArrowDown'?1:-1)+choices.length)%choices.length:-1;highlight();}
    else if(e.key==='Enter'&&!list.hidden&&active>=0){e.preventDefault();choose(active);}
  });
  document.addEventListener('pointerdown',e=>{if(!wrap.contains(e.target)&&!list.contains(e.target))close();});
  document.addEventListener('focusin',e=>{if(!wrap.contains(e.target))close();});
  document.addEventListener('modelsupdated',()=>{if(!list.hidden)draw();});
  document.querySelectorAll('[data-mode]').forEach(button=>button.addEventListener('click',()=>{if(id==='test-model'){input.value='';snippet();close();}}));
  window.addEventListener('resize',close);window.addEventListener('scroll',e=>{if(e.target!==list&&!list.contains(e.target))close();},true);
}
