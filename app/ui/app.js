const $=s=>document.querySelector(s);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));let sortBy='name';let data={apps:[],settings:{}},filter='all',busy=false;
const hue=n=>{let h=0;for(const c of n)h=(h*31+c.charCodeAt(0))%360;return h};
const fmt=b=>b>1e9?(b/1e9).toFixed(1)+' GB':b>1e6?(b/1e6).toFixed(1)+' MB':Math.round(b/1e3)+' KB';
const ago=t=>{if(!t)return 'never';const s=Date.now()/1000-t;return s<90?'just now':s<5400?Math.round(s/60)+' min ago':s<172800?Math.round(s/3600)+' h ago':Math.round(s/86400)+' d ago'};
function toast(m,err){const e=document.createElement('div');e.className='toast'+(err?' error':'');e.textContent=m;$('#toasts').append(e);setTimeout(()=>e.remove(),4500)}
async function api(path,method='GET',body){const r=await fetch('api/'+path,{method,headers:{'X-Requested-With':'crafthub',...(body?{'Content-Type':'application/json'}:{})},body:body?JSON.stringify(body):undefined});
  if(!r.ok){let m=r.statusText;try{m=(await r.json()).detail||m}catch{}throw new Error(m)}return r.json()}
function confirmDlg(t,p,label,danger){return new Promise(res=>{$('#dt').textContent=t;$('#dp').textContent=p;const y=$('#dyes');y.textContent=label;y.className=danger?'danger':'primary';if(danger)y.style.cssText='background:var(--bad);color:#fff;border:0';else y.style.cssText='';
  dlg.showModal();const done=v=>{dlg.close();res(v)};y.onclick=()=>done(true);$('#dno').onclick=()=>done(false);dlg.oncancel=()=>res(false)})}
async function load(){try{data=await api('apps');render()}catch(e){toast(e.message,true)}}
function render(){
  const a=data.apps,inst=a.filter(x=>x.installed),upd=a.filter(x=>x.update_available);
  $('#checked').textContent='Last checked '+ago(data.checked_at)+' · github.com/storytold';
  $('#autoall').checked=!!data.settings.auto_update_all;
  $('#btnupall').hidden=!upd.length;$('#btnupall').textContent='⬆ Update all ('+upd.length+')';
  $('#stats').innerHTML=[['Available',a.length],['Installed',inst.length],['Updates',upd.length],['Disk used',fmt(a.reduce((s,x)=>s+(x.installed?x.installed.size||0:0)+(x.packages_size||0),0))]].map(([l,v])=>`<div class="stat"><b>${v}</b><span>${l}</span></div>`).join('');
  $('#chips').innerHTML=[['all','All'],['installed','Installed'],['updates','Updates'],['avail','Not installed']].map(([k,l])=>`<button class="chip ${filter===k?'on':''}" data-f="${k}">${l}</button>`).join('');
  const pr=Object.entries(data.problems||{});$('#problems').innerHTML=pr.length?`<div class="warnbox">⚠ Couldn't check: ${pr.map(([k,v])=>esc(k)+' ('+esc(v)+')').join(', ')}</div>`:'';
  const q=$('#q').value.toLowerCase();
  const list=a.filter(x=>(filter==='all'||(filter==='installed'&&x.installed)||(filter==='updates'&&x.update_available)||(filter==='avail'&&!x.installed))&&(x.name+x.replaces+x.description).toLowerCase().includes(q));
  list.sort(sortBy==='updated'?(p,q)=>(p.latest_age_hours??1e9)-(q.latest_age_hours??1e9):sortBy==='size'?(p,q)=>((q.installed?.size||0)+(q.packages_size||0))-((p.installed?.size||0)+(p.packages_size||0)):(p,q)=>p.name.localeCompare(q.name));
  $('#grid').innerHTML=list.map(card).join('')||'<div class="empty">Nothing here.</div>';
  busy=a.some(x=>x.job&&x.job.state==='running');
}
function card(x){
  const h=hue(x.name),j=x.job,i=x.installed;
  const fresh=data.fresh_hours>0&&x.latest_age_hours!=null&&x.latest_age_hours<data.fresh_hours;const fp=fresh&&(!i||x.update_available)?`<span class="pill warn" title="Released very recently; consider waiting">⚠ released ${x.latest_age_hours<1?'<1':Math.round(x.latest_age_hours)}h ago</span>`:'';
  let pills=`<span class="pill">${x.latest?'latest '+esc(x.latest):'no web build'}</span>`+fp;
  if(i)pills=`<span class="pill ${x.update_available?'warn':'ok'}">${x.update_available?'● update → '+esc(x.latest):'● '+esc(i.version)}</span>`+(x.update_available?`<span class="pill">installed ${esc(i.version)}</span>`:'')+fp+`<span class="pill">${fmt(i.size)}</span>`+(i.verified?'<span class="pill ok" title="SHA-256 verified">✓ verified</span>':'')+(i.auto_update?'<span class="pill">auto</span>':'');
  let act;
  if(j&&j.state==='running')act=`<div style="width:100%"><div class="prog"><i style="width:${j.pct}%"></i></div><div class="jobmsg">${esc(j.msg)}…</div></div>`;
  else{
    act=(j&&j.state==='error'?`<div class="jobmsg" style="width:100%;color:var(--bad)">${esc(j.msg)} <button class="icon" style="padding:0 6px" data-a="dismiss" data-n="${esc(x.name)}">✕</button></div>`:'');
    if(!i)act+=`<button class="primary" data-a="install" data-n="${esc(x.name)}" ${x.latest?'':'disabled'}>⬇ Install${x.latest_size?' · '+fmt(x.latest_size):''}</button>`;
    else act+=`<a class="btn primary" href="${esc(x.path)}" target="_blank">▶ Open</a>`+(x.update_available?`<button data-a="install" data-n="${esc(x.name)}" title="Update to ${esc(x.latest)}">⬆ Update</button>`:'');
    if(i)act+=`<div class="menu"><button class="icon" data-a="menu" data-n="${esc(x.name)}">⋯</button><div class="pop" id="m-${esc(x.name)}">
      <label><input type="checkbox" data-a="auto" data-n="${esc(x.name)}" ${i.auto_update?'checked':''}> Auto-update</label>
      <select data-a="ver" data-n="${esc(x.name)}"><option value="">Install specific version…</option>${x.versions.map(v=>`<option>${esc(v)}</option>`).join('')}</select>
      ${x.has_previous?`<button data-a="rollback" data-n="${esc(x.name)}">↩ Roll back to ${esc(i.previous||'previous')}</button>`:''}
      <button data-a="copy" data-n="${esc(x.name)}">🔗 Copy link</button>
      <button data-a="openq" data-q="?webgl" data-n="${esc(x.name)}" title="Force the WebGL2 renderer (if the default shows a blank or garbled canvas)">▶ Open with WebGL</button>
      <button data-a="openq" data-q="?cpu" data-n="${esc(x.name)}" title="Force the slowest, most compatible CPU renderer">▶ Open with CPU renderer</button>
      ${x.latest_url?`<button data-a="notes" data-n="${esc(x.name)}">📝 Release notes</button>`:''}
      <button data-a="repo" data-n="${esc(x.name)}">⌥ Source repo</button><hr>
      <button class="danger" data-a="delete" data-n="${esc(x.name)}">🗑 Delete</button></div></div>`;
  }
  return `<div class="card"><div class="top"><div class="ava" style="background:linear-gradient(135deg,hsl(${h} 75% 58%),hsl(${(h+50)%360} 80% 45%))">${esc(x.name[0])}</div><div><div class="name">${esc(x.name)}</div><div class="rep">${x.replaces?'Alternative to '+esc(x.replaces):'WebAssembly app'}</div></div></div>
  <div class="desc">${esc(x.description)}</div><div class="meta">${pills}</div><div class="actions">${act}</div></div>`}
document.addEventListener('click',async e=>{
  const t=e.target.closest('[data-a]');
  if(!e.target.closest('.menu'))document.querySelectorAll('.pop.open').forEach(p=>p.classList.remove('open'));
  if(e.target.dataset.f){filter=e.target.dataset.f;render();return}
  if(!t)return;const n=t.dataset.n,x=data.apps.find(a=>a.name===n),a=t.dataset.a;
  try{
    if(a==='menu'){const p=$('#m-'+n),o=p.classList.contains('open');document.querySelectorAll('.pop.open').forEach(p=>p.classList.remove('open'));if(!o)p.classList.add('open')}
    else if(a==='install'){const hrs=x.latest_age_hours;if(data.fresh_hours>0&&hrs!=null&&hrs<data.fresh_hours&&!await confirmDlg('Very recent release',`${n} ${x.latest} was published only ${hrs<1?'minutes':Math.round(hrs)+' hour(s)'} ago. New releases can have undiscovered problems. Install anyway? (You can roll back afterwards.)`,'Install anyway'))return;await api(`apps/${n}/install`,'POST',{});poll()}
    else if(a==='dismiss'){await api(`apps/${n}/dismiss`,'POST');load()}
    else if(a==='rollback'){if(await confirmDlg('Roll back '+n+'?','Swaps back to the previously installed version.','Roll back')){await api(`apps/${n}/rollback`,'POST');toast(n+' rolled back');load()}}
    else if(a==='delete'){if(await confirmDlg('Delete '+n+'?','Removes the hosted files and any saved previous version. Your documents live in your browser and are not affected. You can reinstall any time.','Delete',true)){await api('apps/'+n,'DELETE');toast(n+' deleted');load()}}
    else if(a==='copy'){await navigator.clipboard.writeText(new URL(x.path,location.href).href);toast('Link copied')}
    else if(a==='openq')window.open(x.path+t.dataset.q);else if(a==='notes')window.open(x.latest_url);else if(a==='repo')window.open(x.repo);
  }catch(err){toast(err.message,true)}
});
document.addEventListener('change',async e=>{const t=e.target,n=t.dataset.n;if(!t.dataset.a)return;try{
  if(t.dataset.a==='auto'){await api(`apps/${n}/settings`,'POST',{auto_update:t.checked});toast('Auto-update '+(t.checked?'on':'off')+' for '+n);load()}
  if(t.dataset.a==='ver'&&t.value){const v=t.value;if(await confirmDlg('Install '+n+' '+v+'?','Replaces the current version (the old one is kept for rollback).','Install')){await api(`apps/${n}/install`,'POST',{version:v});poll()}else t.value=''}
}catch(err){toast(err.message,true)}});
$('#q').oninput=render;$('#sort').onchange=e=>{sortBy=e.target.value;render()};
$('#btnset').onclick=()=>{$('#hours').value=data.settings.check_hours;$('#notifystate').textContent=data.notify?'Notifications: enabled (NOTIFY_URL)':'Notifications: off (set NOTIFY_URL in the Unraid template to enable)';setdlg.showModal()};
$('#saveset').onclick=async()=>{try{await api('settings','POST',{check_hours:+$('#hours').value});toast('Saved');setdlg.close();load()}catch(e){toast(e.message,true)}};
$('#btnexport').onclick=async()=>{const b=new Blob([JSON.stringify(await api('export'),null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='crafthub-apps.json';a.click()};
$('#btnimport').onclick=()=>$('#importfile').click();
$('#importfile').onchange=async e=>{try{const r=await api('import','POST',JSON.parse(await e.target.files[0].text()));toast('Installing '+r.started+' app(s)');setdlg.close();poll()}catch(err){toast('Import failed: '+err.message,true)}e.target.value=''};
$('#autoall').onchange=async e=>{await api('settings','POST',{auto_update_all:e.target.checked});toast('Auto-update all '+(e.target.checked?'on':'off'))};
$('#btncheck').onclick=async()=>{const b=$('#btncheck');b.disabled=true;b.textContent='Checking…';try{data=await api('refresh','POST');render();const u=data.apps.filter(a=>a.update_available).length;toast(u?u+' update(s) available':'Everything is up to date')}catch(e){toast(e.message,true)}b.disabled=false;b.textContent='↻ Check for updates'};
$('#btnupall').onclick=async()=>{const fr=data.apps.filter(a=>a.update_available&&data.fresh_hours>0&&a.latest_age_hours<data.fresh_hours);if(fr.length&&!await confirmDlg('Very recent releases',`${fr.map(a=>a.name).join(', ')} ${fr.length>1?'were':'was'} released less than ${data.fresh_hours} hours ago. Update anyway?`,'Update anyway'))return;const r=await api('upgrade-all','POST');toast('Updating '+r.started+' app(s)');poll()};
$('#btninstall').onclick=async()=>{const todo=data.apps.filter(a=>!a.installed&&a.latest);if(!todo.length)return toast('Everything is installed');
  if(await confirmDlg('Install all?',`Downloads ${todo.length} apps (about ${fmt(todo.reduce((s,a)=>s+(a.latest_size||0),0))}).`,'Install all')){for(const a of todo)await api(`apps/${a.name}/install`,'POST',{}).catch(()=>0);poll()}};
$('#btnlog').onclick=async()=>{const l=await api('log');$('#logbox').innerHTML=l.map(e=>`<div class="logline ${e.level}"><time>${new Date(e.t*1000).toLocaleString()}</time>${esc(e.msg)}</div>`).join('')||'<div class="logline">No activity yet.</div>';logdlg.showModal()};
$('#btntheme').onclick=()=>{const d=document.documentElement;const v=d.dataset.theme==='light'?'dark':'light';d.dataset.theme=v;try{localStorage.theme=v}catch{}};
try{if(localStorage.theme)document.documentElement.dataset.theme=localStorage.theme;else if(matchMedia('(prefers-color-scheme: light)').matches)document.documentElement.dataset.theme='light'}catch{}
let timer;function poll(){clearTimeout(timer);load().then(()=>{timer=setTimeout(poll,busy?800:30000)})}
poll();

$('#logclose').onclick=()=>logdlg.close();$('#setclose').onclick=()=>setdlg.close();
