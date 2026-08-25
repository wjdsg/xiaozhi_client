(function(){
  'use strict';
  const list=document.getElementById('recordList'),detail=document.getElementById('recordDetail'),content=document.getElementById('detailContent'),meta=document.getElementById('detailMeta'),photoViewer=document.getElementById('photoViewer'),photoViewerImage=document.getElementById('photoViewerImage');
  let current=null,knownNewest='';
  const statusTime=document.getElementById('statusTime');
  function updateStatusTime(){if(!statusTime)return;const now=new Date(),pad=value=>String(value).padStart(2,'0');statusTime.textContent=pad(now.getHours())+':'+pad(now.getMinutes());}
  const esc=value=>String(value??'').replace(/[&<>"']/g,ch=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const dateText=value=>{let d=new Date(value);return Number.isNaN(d.getTime())?'刚刚':new Intl.DateTimeFormat('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}).format(d);};
  const languageText=value=>String(value).toLowerCase().startsWith('en')?'英语听写':'中文听写';
  async function getJson(url){let response=await fetch(url,{cache:'no-store'}),payload=await response.json();if(!response.ok)throw Error(payload.error||'暂时无法同步');return payload;}
  function renderList(records){
    if(!records.length){list.innerHTML='<div class="empty-card"><strong>还没有听写记录</strong><p>孩子在台灯完成听写后，可以选择“拍照上传”。</p></div>';return;}
    list.innerHTML=records.map(record=>'<button class="record-card" data-record="'+esc(record.id)+'"><img class="record-cover" src="'+esc(record.coverUrl)+'" alt="听写结果照片"><span class="record-copy"><b>'+languageText(record.language)+'</b><span>'+record.wordCount+' 个词 · '+record.photoCount+' 张照片</span><small>'+dateText(record.createdAt)+'</small></span><span class="record-arrow">›</span></button>').join('');
    list.querySelectorAll('[data-record]').forEach(button=>button.onclick=()=>openRecord(button.dataset.record));
  }
  async function loadRecords(silent){
    try{let payload=await getJson('/api/parent/records'),records=payload.items||[];renderList(records);if(records[0]&&records[0].id!==knownNewest){knownNewest=records[0].id;}}
    catch(error){if(!silent)list.innerHTML='<div class="error-card">'+esc(error.message)+'</div>';}
  }
  function renderDetail(){
    if(!current)return;
    const photos=current.photos||[],words=current.words||[];
    content.innerHTML='<section class="detail-section"><div class="detail-section-title"><b>听写照片</b><span>'+photos.length+' 张</span></div><div class="photo-stack">'+(photos.length?photos.map((photo,index)=>'<figure><button class="photo-preview-button" data-preview-photo="'+esc(photo.url)+'" data-preview-alt="第'+(index+1)+'张听写照片"><img src="'+esc(photo.url)+'" alt="第'+(index+1)+'张听写照片"></button><figcaption><span>第 '+(index+1)+' 张</span><button class="photo-delete-button" data-delete-photo="'+esc(photo.name)+'">删除</button></figcaption></figure>').join(''):'<div class="empty-card"><strong>暂无上传照片</strong></div>')+'</div></section><section class="detail-section"><div class="detail-section-title"><b>标准词表</b><span>按播报顺序</span></div><div class="word-summary"><span>本次听写词语</span><b>'+words.length+' 个</b></div><div class="word-grid">'+words.map(word=>'<div class="word-item"><b title="'+esc(word.text)+'">'+esc(word.text)+'</b></div>').join('')+'</div></section>';
    content.querySelectorAll('[data-preview-photo]').forEach(button=>button.onclick=()=>openPhotoViewer(button.dataset.previewPhoto,button.dataset.previewAlt));
    content.querySelectorAll('[data-delete-photo]').forEach(button=>button.onclick=()=>deletePhoto(button.dataset.deletePhoto));
  }
  async function openRecord(id){
    detail.classList.add('open');detail.setAttribute('aria-hidden','false');content.innerHTML='<div class="loading-card"><span></span><p>正在打开听写记录</p></div>';
    try{current=await getJson('/api/parent/records/'+encodeURIComponent(id));meta.textContent=languageText(current.language)+' · '+current.words.length+'个 · '+dateText(current.createdAt);renderDetail();}
    catch(error){content.innerHTML='<div class="error-card">'+esc(error.message)+'</div>';}
  }
  async function deletePhoto(filename){
    if(!current||!window.confirm('确定删除这张上传照片吗？'))return;
    try{
      let response=await fetch('/api/parent/records/'+encodeURIComponent(current.id)+'/photos/'+encodeURIComponent(filename),{method:'DELETE',cache:'no-store'}),payload=await response.json().catch(()=>({}));
      if(response.status===405)throw Error('家长端服务需要重启后才支持删除照片');
      if(!response.ok)throw Error(payload.error||'照片删除失败');
      if(payload.deleted){detail.classList.remove('open');detail.setAttribute('aria-hidden','true');current=null;await loadRecords(false);return;}
      current=payload.record;renderDetail();await loadRecords(true);
    }catch(error){content.querySelectorAll('.detail-error').forEach(node=>node.remove());content.insertAdjacentHTML('afterbegin','<div class="error-card detail-error">'+esc(error.message)+'</div>');}
  }
  function openPhotoViewer(url,alt){if(!photoViewer||!photoViewerImage)return;photoViewerImage.src=url;photoViewerImage.alt=alt||'听写照片';photoViewer.classList.add('open');photoViewer.setAttribute('aria-hidden','false');}
  function closePhotoViewer(){if(!photoViewer)return;photoViewer.classList.remove('open');photoViewer.setAttribute('aria-hidden','true');photoViewerImage.removeAttribute('src');}
  document.getElementById('detailBack').onclick=()=>{closePhotoViewer();detail.classList.remove('open');detail.setAttribute('aria-hidden','true');current=null;};
  document.getElementById('photoViewerClose').onclick=closePhotoViewer;
  photoViewer.onclick=event=>{if(event.target===photoViewer)closePhotoViewer();};
  document.getElementById('refreshButton').onclick=()=>loadRecords(false);
  updateStatusTime();
  setInterval(updateStatusTime,1000);
  loadRecords(false);
  setInterval(()=>{if(!current)loadRecords(true);},4000);
})();
